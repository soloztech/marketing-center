"""Paged convergence of two independently ordered, durable authorities."""

import datetime
import logging

from psycopg2 import OperationalError

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from odoo.addons.marketing_center_base.services.serialization import (
    acquire_advisory_xact_lock,
)

from .journey_capture import CAPTURE_VERSION

_logger = logging.getLogger(__name__)
AUTHORITY = "website.whatsapp"
BATCH = 100
WITHDRAWAL_RETRIES = 5


class WhatsappJourneyService(models.AbstractModel):
    _name = "marketing.website.whatsapp.journey.service"
    _description = "Website WhatsApp business journey convergence"

    @api.model
    def _source_ref(self, match, link):
        return "website.whatsapp:match:%s:link:%s" % (match.id, link.id)

    @api.model
    def _lock_graph(self, links, channels):
        lead_ids = set(links.sudo().mapped("lead_id").ids)
        lead_ids.update(links.sudo().mapped("lead_record_id_snapshot"))
        if links:
            # A context transfer can preserve the generation ID but replace its
            # lead snapshot. Fence the still-supported old businesses as well,
            # before touching any assertion or acquiring a match lock.
            self.env["marketing.attribution.crm.link"].flush_model()
            self.env.cr.execute(
                "SELECT DISTINCT assertion.lead_id "
                "FROM marketing_attribution_crm_link assertion "
                "WHERE assertion.authority_key = %s "
                "AND assertion.company_id = ANY(%s) "
                "AND substring(assertion.source_ref from ':link:([0-9]+)$') = ANY(%s) "
                "AND NOT EXISTS (SELECT 1 FROM marketing_attribution_crm_revocation revoked "
                "WHERE revoked.assertion_id = assertion.id)",
                [
                    AUTHORITY,
                    links.company_id.ids,
                    [str(record_id) for record_id in links.ids],
                ],
            )
            lead_ids.update(row[0] for row in self.env.cr.fetchall() if row[0])
        return (
            self.env["crm.lead"]
            .sudo()
            .browse(sorted(lead_ids - {False, 0}))
            .exists()
            ._contact_center_lock_conversation_graph(
                channel_ids=channels.ids, touch_leads=True, touch_channels=True
            )
        )

    @api.model
    def _lock_match(self, match):
        # Every caller has already acquired the Contact graph. The correlator
        # takes account -> handoff -> match; workers never acquire account.
        acquire_advisory_xact_lock(
            self.env.cr, "website_whatsapp_journey:match:%s" % match.id
        )
        self.env.cr.execute(
            "SELECT id FROM marketing_website_whatsapp_handoff WHERE id=%s FOR UPDATE",
            [match.handoff_id.id],
        )
        self.env.cr.execute(
            "SELECT id FROM marketing_website_whatsapp_match WHERE id=%s FOR UPDATE",
            [match.id],
        )
        match.invalidate_recordset()
        match.handoff_id.invalidate_recordset()
        return match.exists()

    @api.model
    def _existing_point(self, handoff):
        point = handoff.sudo().journey_touchpoint_id
        return (
            point
            if point and not point.privacy_erased_at
            else self.env["marketing.attribution.touchpoint"]
        )

    @api.model
    def _project_claim(self, match):
        """Only the claim worker creates evidence, even with no business yet."""
        handoff = match.handoff_id.sudo()
        point = handoff.journey_touchpoint_id
        if point or not match._journey_claim_valid():
            return self._existing_point(handoff)
        if handoff._journey_privacy_status(first_projection=True) != "ready":
            return self.env["marketing.attribution.touchpoint"]
        # The old organic-link producer has its own occurrence/retention. A
        # replay across capture modes must not replace or duplicate that event.
        if handoff._journey_legacy_event():
            return self.env["marketing.attribution.touchpoint"]
        dto = handoff._journey_dto()
        points = self.env["marketing.attribution.touchpoint"].sudo()
        point = points.search(
            [
                ("company_id", "=", handoff.company_id.id),
                ("canonical_key", "=", dto.canonical_key),
            ],
            order="revision_sequence desc,id desc",
            limit=1,
        )
        if not point:
            result = self.env["marketing.attribution.service"]._ingest_touchpoint(
                handoff.company_id, dto
            )
            point = points.browse(result.touchpoint_id)
        handoff._service().write({"journey_touchpoint_id": point.id})
        return point if not point.privacy_erased_at else points.browse()

    @api.model
    def _pair(self, match, link):
        match.ensure_one()
        link.ensure_one()
        if match.company_id != link.company_id or match.channel_id != link.channel_id:
            return
        source_ref = self._source_ref(match, link)
        crm = self.env["marketing.crm.service"]
        assertions = (
            self.env["marketing.attribution.crm.link"]
            .sudo()
            .search(
                [
                    ("company_id", "=", match.company_id.id),
                    ("authority_key", "=", AUTHORITY),
                    ("authority_ref", "=", str(match.id)),
                    ("source_ref", "=", source_ref),
                    ("revocation_ids", "=", False),
                ]
            )
        )
        point = self._existing_point(match.handoff_id)
        stale = assertions.filtered(
            lambda row: link.state == "active"
            and link.lead_id
            and row.lead_id != link.lead_id
        )
        if stale:
            crm._revoke_attribution_assertions(
                stale,
                AUTHORITY,
                str(match.id),
                source_ref + ":transferred",
                reason="website_whatsapp_business_changed",
            )
            stale.mapped("lead_id")._enqueue_native_utm()
            assertions -= stale
        invalid = (
            not match._journey_claim_valid()
            or link.state != "active"
            or not link.lead_id
            or match.handoff_id._journey_privacy_status() != "ready"
        )
        if invalid:
            if assertions:
                crm._revoke_attribution_assertions(
                    assertions,
                    AUTHORITY,
                    str(match.id),
                    "website.whatsapp:match:%s:link:%s:retired" % (match.id, link.id),
                    reason="website_whatsapp_authority_removed",
                )
                assertions.mapped("lead_id")._enqueue_native_utm()
            return
        # Clearing the lead company suspends credit immediately in the shared
        # predicate. Preserve an otherwise valid generation so restoring the
        # company can replay it; a revoked idempotency key cannot be resurrected.
        # Reject/unlink/privacy above still retire authority even in that state.
        if not match._journey_business_company_valid(link):
            return
        if not point or match.handoff_id.capture_version != CAPTURE_VERSION:
            return
        crm._link_touchpoint_lead(
            point,
            link.lead_id,
            source_ref=source_ref,
            authority_key=AUTHORITY,
            authority_ref=str(match.id),
            assertion_ref="website.whatsapp:match:%s:link:%s:lead:%s"
            % (match.id, link.id, link.lead_id.id),
        )
        link.lead_id._enqueue_native_utm()


class Company(models.Model):
    _inherit = "res.company"

    def _whatsapp_job_scope(self):
        self.ensure_one()
        if self not in self.env.companies:
            raise AccessError(_("The journey company is not available."))
        return (
            self.sudo().with_context(allowed_company_ids=[self.id]).with_company(self)
        )

    def _job_whatsapp_match(self, match_id, revision, after_id=0, wake="transition"):
        company = self._whatsapp_job_scope()
        env = company.env
        match = env["marketing.website.whatsapp.match"].browse(match_id).exists()
        if not match or match.company_id != company:
            return False
        links = env["contact.center.crm.conversation.link"].search(
            [
                ("channel_id", "=", match.channel_id.id),
                ("company_id", "=", company.id),
                ("id", ">", after_id),
            ],
            order="id",
            limit=BATCH + 1,
        )
        page = links[:BATCH]
        service = env["marketing.website.whatsapp.journey.service"]
        locked = service._lock_graph(page, match.channel_id)
        service = service.with_context(**locked.env.context)
        match = service._lock_match(match)
        if not match or match.journey_revision != revision:
            return False
        # Do this before the link page, including an empty page.
        service._project_claim(match)
        page.invalidate_recordset()
        for link in page.exists():
            service._pair(match, link)
        if len(links) > BATCH:
            company.with_delay(
                identity_key="whatsapp:match:%s:%s:%s:%s"
                % (match.id, revision, page[-1].id, wake),
                max_retries=5,
            )._job_whatsapp_match(match.id, revision, page[-1].id, wake=wake)
        else:
            match._service().write({"journey_pending": False})
            # Earlier pages may have classified while the shared claim marker
            # was pending. Wake every current business after that marker clears.
            company.with_delay(
                identity_key="whatsapp:wake:%s:%s:0" % (match.id, revision),
                max_retries=5,
            )._job_whatsapp_wake(match.id, revision)
        return True

    def _job_whatsapp_wake(self, match_id, revision, after_id=0):
        company = self._whatsapp_job_scope()
        match = (
            company.env["marketing.website.whatsapp.match"].browse(match_id).exists()
        )
        if (
            not match
            or match.company_id != company
            or match.journey_revision != revision
        ):
            return False
        links = company.env["contact.center.crm.conversation.link"].search(
            [
                ("company_id", "=", company.id),
                ("channel_id", "=", match.channel_id.id),
                ("state", "=", "active"),
                ("id", ">", after_id),
            ],
            order="id",
            limit=BATCH + 1,
        )
        links[:BATCH].mapped("lead_id")._enqueue_native_utm()
        if len(links) > BATCH:
            company.with_delay(
                identity_key="whatsapp:wake:%s:%s:%s"
                % (match.id, revision, links[BATCH - 1].id),
                max_retries=5,
            )._job_whatsapp_wake(match.id, revision, links[BATCH - 1].id)
        return True

    def _job_whatsapp_link(self, link_id, revision, after_id=0):
        company = self._whatsapp_job_scope()
        env = company.env
        link = env["contact.center.crm.conversation.link"].browse(link_id).exists()
        if not link or link.company_id != company:
            return False
        service = env["marketing.website.whatsapp.journey.service"]
        locked = service._lock_graph(link, link.channel_id)
        service = service.with_context(**locked.env.context)
        link.invalidate_recordset()
        if link.website_journey_revision != revision:
            return False
        matches = env["marketing.website.whatsapp.match"].search(
            [
                ("channel_id", "=", link.channel_id.id),
                ("company_id", "=", company.id),
                ("id", ">", after_id),
            ],
            order="id",
            limit=BATCH + 1,
        )
        page = matches[:BATCH]
        for match in page:
            match = service._lock_match(match)
            if match:
                service._pair(match, link)
        if len(matches) > BATCH:
            company.with_delay(
                identity_key="whatsapp:link:%s:%s:%s"
                % (link.id, revision, page[-1].id),
                max_retries=5,
            )._job_whatsapp_link(link.id, revision, page[-1].id)
        else:
            link._service().write({"website_journey_pending": False})
            link.mapped("lead_id")._enqueue_native_utm()
        return True

    def _enqueue_whatsapp_erasure(self, handoff):
        self.ensure_one()
        self._whatsapp_job_scope().with_delay(
            identity_key="whatsapp:erasure:%s" % handoff.id,
            max_retries=5,
        )._job_whatsapp_erasure(handoff.id)

    def _job_whatsapp_erasure(self, handoff_id, after_id=0):
        company = self._whatsapp_job_scope()
        matches = company.env["marketing.website.whatsapp.match"].search(
            [
                ("company_id", "=", company.id),
                ("handoff_id", "=", handoff_id),
                ("id", ">", after_id),
            ],
            order="id",
            limit=BATCH + 1,
        )
        for match in matches[:BATCH]:
            match._enqueue_journey(wake="erasure:%s" % handoff_id)
        if len(matches) > BATCH:
            company.with_delay(
                identity_key="whatsapp:erasure:%s:%s"
                % (handoff_id, matches[BATCH - 1].id),
                max_retries=5,
            )._job_whatsapp_erasure(handoff_id, matches[BATCH - 1].id)
        return True

    def _job_whatsapp_withdrawal(self, decision_id, after_id=0, attempt=0):
        company = self._whatsapp_job_scope()
        domain = [
            ("company_id", "=", company.id),
            ("capture_consent_id", "=", decision_id),
            ("capture_version", "=", CAPTURE_VERSION),
            ("erased_at", "=", False),
        ]
        handoffs = company.env["marketing.website.whatsapp.handoff"].search(
            domain + [("id", ">", after_id)],
            order="id",
            limit=BATCH + 1,
        )
        for handoff in handoffs[:BATCH]:
            try:
                with self.env.cr.savepoint():
                    handoff._erase_journey_values()
            except OperationalError:
                raise
            except Exception as error:
                _logger.warning(
                    "Website WhatsApp withdrawal failed (%s)", type(error).__name__
                )
                if attempt >= WITHDRAWAL_RETRIES:
                    # Native Failed jobs remain visible and can be requeued
                    # after an operator fixes a persistent data/hook failure.
                    raise
                handoff._record_retention_failure()
        if len(handoffs) > BATCH:
            company.with_delay(
                identity_key="whatsapp:withdrawal:%s:%s:%s"
                % (decision_id, attempt, handoffs[BATCH - 1].id),
                max_retries=5,
            )._job_whatsapp_withdrawal(decision_id, handoffs[BATCH - 1].id, attempt)
        elif attempt < WITHDRAWAL_RETRIES and company.env[
            "marketing.website.whatsapp.handoff"
        ].search(domain, limit=1):
            # Commit successful siblings; retry failed captures from cursor zero
            # in a fresh transaction, even after the browser discarded consent.
            company.with_delay(
                identity_key="whatsapp:withdrawal:%s:retry:%s"
                % (decision_id, attempt + 1),
                eta=fields.Datetime.now()
                + datetime.timedelta(seconds=60 * (attempt + 1)),
                max_retries=5,
            )._job_whatsapp_withdrawal(decision_id, 0, attempt + 1)
        return True

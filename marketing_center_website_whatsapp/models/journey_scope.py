"""Website authority follows the current conversation business generation."""
# Preserve optional cooperative ORM extensions.
# pylint: disable=consider-merging-classes-inherited

import re

from odoo import api, fields, models

from .correlation import CLAIMED_STATES
from .journey_capture import CAPTURE_VERSION
from .journey_service import AUTHORITY

SOURCE = re.compile(r"^website\.whatsapp:match:([1-9][0-9]*):link:([1-9][0-9]*)$")


class WebsiteWhatsappMatch(models.Model):
    _inherit = "marketing.website.whatsapp.match"

    journey_revision = fields.Integer(default=0, readonly=True, copy=False)
    journey_pending = fields.Boolean(readonly=True, copy=False)
    erased_at = fields.Datetime(related="handoff_id.erased_at", readonly=True)

    def _journey_claim_valid(self):
        self.ensure_one()
        message = self.sudo().message_binding_id
        return bool(
            self.state in CLAIMED_STATES
            and message
            and message.company_id == self.company_id
            and message.account_id == self.account_id
            and message.channel_binding_id.channel_id == self.channel_id
            and self.channel_id.contact_center_company_id == self.company_id
            and message.direction == "inbound"
            and message.origin == "provider"
            # The received claim is historical evidence. Later provider edits
            # or deletion do not silently retract it; explicit review does.
            and message.channel_binding_id.conversation_type == "direct"
            and message.account_id.platform == "whatsapp"
            and self.message_at
        )

    def _journey_business_company_valid(self, link):
        self.ensure_one()
        return bool(
            link.lead_id
            and link.lead_id.company_id == self.company_id
            and (
                not link.lead_id.marketing_event_company_id
                or link.lead_id.marketing_event_company_id == self.company_id
            )
        )

    def _journey_has_support(self, link):
        self.ensure_one()
        point = self.handoff_id.sudo().journey_touchpoint_id
        return bool(
            point
            and self.env["marketing.attribution.crm.link"]
            .sudo()
            .search_count(
                [
                    ("company_id", "=", self.company_id.id),
                    ("lead_id", "=", link.lead_id.id),
                    ("canonical_key", "=", point.canonical_key),
                    ("authority_key", "=", AUTHORITY),
                    ("authority_ref", "=", str(self.id)),
                    (
                        "source_ref",
                        "=",
                        "website.whatsapp:match:%s:link:%s" % (self.id, link.id),
                    ),
                    ("revocation_ids", "=", False),
                ]
            )
        )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records.filtered(lambda row: row.state in CLAIMED_STATES)._enqueue_journey()
        return records

    def write(self, values):
        result = super().write(values)
        if "state" in values:
            self._enqueue_journey()
        return result

    def _enqueue_journey(self, wake="transition"):
        for record in self._service():
            if record.handoff_id.capture_version != CAPTURE_VERSION:
                continue
            revision = record.journey_revision + 1
            record.write({"journey_revision": revision, "journey_pending": True})
            company = (
                record.company_id.sudo()
                .with_context(allowed_company_ids=record.company_id.ids)
                .with_company(record.company_id)
            )
            company.with_delay(
                identity_key="whatsapp:match:%s:%s:0:%s" % (record.id, revision, wake),
                max_retries=5,
            )._job_whatsapp_match(record.id, revision, wake=wake)


class ConversationLink(models.Model):
    _inherit = "contact.center.crm.conversation.link"

    website_journey_revision = fields.Integer(default=0, readonly=True, copy=False)
    website_journey_pending = fields.Boolean(readonly=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._enqueue_website_journey()
        return records

    def _enqueue_website_journey(self):
        for record in self._service():
            revision = record.website_journey_revision + 1
            pending = bool(
                self.env["marketing.website.whatsapp.match"]
                .sudo()
                .search_count(
                    [
                        ("company_id", "=", record.company_id.id),
                        ("channel_id", "=", record.channel_id.id),
                        ("handoff_id.capture_version", "=", CAPTURE_VERSION),
                    ]
                )
            )
            record.write(
                {
                    "website_journey_revision": revision,
                    "website_journey_pending": pending,
                }
            )
            company = (
                record.company_id.sudo()
                .with_context(allowed_company_ids=record.company_id.ids)
                .with_company(record.company_id)
            )
            company.with_delay(
                identity_key="whatsapp:link:%s:%s:0" % (record.id, revision),
                max_retries=5,
            )._job_whatsapp_link(record.id, revision)

    def _transfer_to_lead(self, lead):
        # Retain the old business fence until the worker revokes its generation.
        old = self.mapped("lead_id")
        result = super()._transfer_to_lead(lead)
        self.filtered(lambda row: row.state == "active")._enqueue_website_journey()
        old._enqueue_native_utm()
        return result

    def _before_tombstone(self, reason):
        result = super()._before_tombstone(reason)
        self._enqueue_website_journey()
        return result


class EffectiveLink(models.Model):
    _inherit = "marketing.attribution.crm.effective.link"

    def _assertion_scope(self, assertion):
        self.ensure_one()
        if assertion.authority_key != AUTHORITY:
            return super()._assertion_scope(assertion)
        root = assertion.derived_from_assertion_id or assertion
        parsed = SOURCE.fullmatch(root.source_ref or "")
        if not parsed or root.authority_ref != parsed[1]:
            return "pending"
        match = (
            self.env["marketing.website.whatsapp.match"]
            .sudo()
            .browse(int(parsed[1]))
            .exists()
        )
        row = (
            self.env["contact.center.crm.conversation.link"]
            .sudo()
            .browse(int(parsed[2]))
            .exists()
        )
        if not match or not row:
            return "pending"
        if (
            match.company_id != self.company_id
            or row.company_id != self.company_id
            or match.channel_id != row.channel_id
            or not match._journey_claim_valid()
            or row.state != "active"
            or row.lead_id != self.lead_id
            or not match._journey_business_company_valid(row)
            or match.handoff_id._journey_privacy_status() != "ready"
            or match.handoff_id.journey_touchpoint_id.canonical_key
            != self.canonical_key
        ):
            return "ineligible"
        if (
            row.scope_state != "confirmed"
            or match.journey_pending
            or row.website_journey_pending
        ):
            return "pending"
        return row._crm_origin_evidence_scope(match.message_at, self.canonical_key)


class CrmLead(models.Model):
    _inherit = "crm.lead"

    def write(self, values):
        company_inputs = {"company_id", "user_id", "team_id", "partner_id"}
        if not self or not company_inputs.intersection(values):
            return super().write(values)
        self.check_access_rights("write")
        self.check_access_rule("write")
        if not self._conversation_links():
            return super().write(values)
        # Native CRM may recompute company from salesperson/team/customer. Fence
        # the same graph before the write and compare the resulting company.
        leads = self._marketing_contact_center_lock_graph(
            touch_leads=True, touch_channels=True
        )
        linked_ids = set(leads._conversation_links().lead_id.ids)
        linked = leads.sudo().filtered(lambda lead: lead.id in linked_ids)
        previous = {lead.id: lead.company_id.id for lead in linked}
        result = super(CrmLead, leads).write(values)
        # A legitimate write can reassign the seller and remove the caller's
        # subsequent read access. Only this private post-write comparison uses
        # sudo; the actual write retains the caller's permissions.
        changed = linked.filtered(lambda lead: lead.company_id.id != previous[lead.id])
        # First convergence may have run while company-less and created no
        # support. Company restoration must wake the existing bounded worker.
        changed._conversation_links()._enqueue_website_journey()
        return result

    def _marketing_scope_review_pending(self):
        self.ensure_one()
        links = self._conversation_links()
        if links.filtered("website_journey_pending"):
            return True
        if (
            self.env["marketing.website.whatsapp.match"]
            .sudo()
            .search_count(
                [
                    ("company_id", "in", links.company_id.ids),
                    ("channel_id", "in", links.channel_id.ids),
                    ("journey_pending", "=", True),
                ]
            )
        ):
            return True
        return super()._marketing_scope_review_pending()

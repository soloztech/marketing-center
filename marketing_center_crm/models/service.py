import hashlib

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.marketing_center_base.services import MarketingBusinessEventDTO
from odoo.addons.marketing_center_base.services.serialization import (
    acquire_advisory_xact_lock,
)

from .tokens import MARKETING_CRM_EVENT_WRITE_TOKEN, MARKETING_CRM_LINK_WRITE_TOKEN


class MarketingCrmService(models.AbstractModel):
    _name = "marketing.crm.service"
    _description = "Marketing CRM Integration Service"

    @api.model
    def _company_for_lead(self, lead):
        lead = lead.exists()
        if getattr(lead, "_name", "") != "crm.lead" or len(lead) != 1:
            raise ValidationError(_("A single valid CRM lead is required."))
        self.env.cr.execute(
            "SELECT id FROM crm_lead WHERE id = %s FOR UPDATE", [lead.id]
        )
        lead.invalidate_recordset(["company_id", "marketing_event_company_id"])
        company = lead.marketing_event_company_id or lead.company_id or self.env.company
        if company not in self.env.companies:
            raise AccessError(_("The CRM lead company is not available."))
        if lead.company_id and lead.company_id != company:
            raise ValidationError(
                _("The CRM lead company conflicts with its immutable marketing scope.")
            )
        if not lead.marketing_event_company_id:
            lead.with_context(
                marketing_crm_event_write_token=MARKETING_CRM_EVENT_WRITE_TOKEN
            ).write({"marketing_event_company_id": company.id})
        return company

    @api.model
    def _assertion_text(self, value, label, size):
        value = (value or "").strip()
        if not value or len(value) > size:
            raise ValidationError(_("The CRM assertion %s is invalid.") % label)
        return value

    @api.model
    def _lead_snapshot_values(self, lead, prefix="lead"):
        """Return the minimum immutable identity needed after lead deletion."""

        lead = lead.exists()
        if getattr(lead, "_name", "") != "crm.lead" or len(lead) != 1:
            raise ValidationError(_("A single valid CRM lead is required."))
        display_ref = (lead.name or "CRM Lead #%s" % lead.id).strip()[:256]
        return {
            "%s_model" % prefix: "crm.lead",
            "%s_res_id" % prefix: lead.id,
            "%s_display_ref" % prefix: display_ref,
        }

    @api.model
    def _default_assertion_ref(
        self, company, touchpoint, lead, authority_key, authority_ref
    ):
        material = "\x1f".join(
            (
                str(company.id),
                touchpoint.canonical_key,
                str(lead.id),
                authority_key,
                authority_ref,
            )
        )
        return "assertion:%s" % hashlib.sha256(material.encode("utf-8")).hexdigest()

    @api.model
    def _link_touchpoint_lead(
        self,
        touchpoint,
        lead,
        source_ref="manual",
        authority_key="marketing.crm",
        authority_ref=None,
        assertion_ref=None,
        derived_from_assertion=None,
    ):
        """Append one authority assertion for a canonical touchpoint/lead link.

        Idempotency belongs to the authority assertion, not to the projected
        touchpoint/lead pair.  Consequently, a second authority always receives
        its own immutable row while an exact replay returns the original row.
        """

        touchpoint = touchpoint.exists()
        if (
            getattr(touchpoint, "_name", "") != "marketing.attribution.touchpoint"
            or len(touchpoint) != 1
        ):
            raise ValidationError(_("A single valid marketing touchpoint is required."))
        company = self._company_for_lead(lead)
        if touchpoint.company_id != company:
            raise ValidationError(
                _("Marketing touchpoints and CRM leads cannot cross companies.")
            )
        source_ref = self._assertion_text(source_ref or "manual", "source", 512)
        authority_key = self._assertion_text(authority_key, "authority", 128)
        authority_ref = self._assertion_text(
            authority_ref or source_ref, "authority reference", 512
        )
        assertion_ref = self._assertion_text(
            assertion_ref
            or self._default_assertion_ref(
                company, touchpoint, lead, authority_key, authority_ref
            ),
            "idempotency reference",
            256,
        )
        derived_from_assertion = (
            derived_from_assertion.sudo().exists()
            if derived_from_assertion
            else self.env["marketing.attribution.crm.link"]
        )
        if derived_from_assertion:
            if (
                getattr(derived_from_assertion, "_name", "")
                != "marketing.attribution.crm.link"
                or len(derived_from_assertion) != 1
                or derived_from_assertion.company_id != company
                or derived_from_assertion.canonical_key != touchpoint.canonical_key
            ):
                raise ValidationError(_("The CRM merge assertion source is invalid."))
            derived_from_assertion = (
                derived_from_assertion.derived_from_assertion_id
                or derived_from_assertion
            )
        lock_key = "marketing_crm_assertion:%s:%s:%s" % (
            company.id,
            authority_key,
            assertion_ref,
        )
        acquire_advisory_xact_lock(self.env.cr, lock_key)
        Link = self.env["marketing.attribution.crm.link"].sudo()
        link = Link.search(
            [
                ("company_id", "=", company.id),
                ("authority_key", "=", authority_key),
                ("assertion_ref", "=", assertion_ref),
            ],
            limit=1,
        )
        if link:
            if (
                link.canonical_key != touchpoint.canonical_key
                or link.lead_id != lead
                or link.authority_ref != authority_ref
                or link.source_ref != source_ref
                or link.derived_from_assertion_id != derived_from_assertion
            ):
                raise ValidationError(
                    _(
                        "The CRM assertion idempotency reference was already used "
                        "for different evidence."
                    )
                )
            return link
        return (
            Link.with_company(company)
            .with_context(marketing_crm_link_write_token=MARKETING_CRM_LINK_WRITE_TOKEN)
            .create(
                {
                    "company_id": company.id,
                    "touchpoint_id": touchpoint.id,
                    "lead_id": lead.id,
                    **self._lead_snapshot_values(lead),
                    "canonical_key": touchpoint.canonical_key,
                    "authority_key": authority_key,
                    "authority_ref": authority_ref,
                    "assertion_ref": assertion_ref,
                    "source_ref": source_ref,
                    "derived_from_assertion_id": derived_from_assertion.id or False,
                }
            )
        )

    @api.model
    def _revoke_attribution_assertions(
        self,
        assertions,
        authority_key,
        authority_ref,
        revocation_ref,
        reason="authority_removed",
    ):
        """Append revocations for assertions owned by exactly one authority."""

        if getattr(assertions, "_name", "") != "marketing.attribution.crm.link":
            raise ValidationError(_("Valid CRM attribution assertions are required."))
        assertions = assertions.sudo().exists()
        authority_key = self._assertion_text(authority_key, "authority", 128)
        authority_ref = self._assertion_text(authority_ref, "authority reference", 512)
        revocation_ref = self._assertion_text(
            revocation_ref, "revocation reference", 512
        )
        reason = self._assertion_text(reason, "revocation reason", 512)
        if any(
            assertion.authority_key != authority_key
            or assertion.authority_ref != authority_ref
            for assertion in assertions
        ):
            raise ValidationError(
                _("A CRM assertion can only be revoked by its own authority.")
            )
        if any(
            assertion.company_id not in self.env.companies for assertion in assertions
        ):
            raise AccessError(_("The CRM assertion company is not available."))
        Revocation = self.env["marketing.attribution.crm.revocation"].sudo()
        result = Revocation.browse()
        pending = []
        for assertion in assertions.sorted("id"):
            company = assertion.company_id
            item_ref = (
                "revoke:%s"
                % hashlib.sha256(
                    ("%s\x1f%s" % (revocation_ref, assertion.id)).encode("utf-8")
                ).hexdigest()
            )
            lock_key = "marketing_crm_revocation:%s:%s" % (
                company.id,
                assertion.id,
            )
            acquire_advisory_xact_lock(self.env.cr, lock_key)
            existing = Revocation.search([("assertion_id", "=", assertion.id)], limit=1)
            if existing:
                if existing.revocation_ref != item_ref or existing.reason != reason:
                    raise ValidationError(
                        _(
                            "The CRM assertion was already revoked by a different "
                            "authority occurrence."
                        )
                    )
                result |= existing
                continue
            reused = Revocation.search(
                [
                    ("company_id", "=", company.id),
                    ("authority_key", "=", authority_key),
                    ("revocation_ref", "=", item_ref),
                ],
                limit=1,
            )
            if reused:
                raise ValidationError(
                    _(
                        "The CRM revocation idempotency reference was already used "
                        "for another assertion."
                    )
                )
            pending.append(
                (
                    company,
                    {
                        "company_id": company.id,
                        "assertion_id": assertion.id,
                        "lead_id": assertion.lead_id.id or False,
                        "lead_model": assertion.lead_model,
                        "lead_res_id": assertion.lead_res_id,
                        "lead_display_ref": assertion.lead_display_ref,
                        "authority_key": authority_key,
                        "authority_ref": authority_ref,
                        "revocation_ref": item_ref,
                        "reason": reason,
                    },
                )
            )
        for company, values in pending:
            result |= (
                Revocation.with_company(company)
                .with_context(
                    marketing_crm_link_write_token=MARKETING_CRM_LINK_WRITE_TOKEN
                )
                .create(values)
            )
        return result

    @api.model
    def _revoke_authority_assertions(
        self,
        lead,
        authority_key,
        authority_ref,
        revocation_ref,
        reason="authority_removed",
    ):
        """Revoke all still-active assertions for one lead authority record."""

        company = self._company_for_lead(lead)
        authority_key = self._assertion_text(authority_key, "authority", 128)
        authority_ref = self._assertion_text(authority_ref, "authority reference", 512)
        assertions = (
            self.env["marketing.attribution.crm.link"]
            .sudo()
            .with_company(company)
            .search(
                [
                    ("company_id", "=", company.id),
                    ("lead_id", "=", lead.id),
                    ("authority_key", "=", authority_key),
                    ("authority_ref", "=", authority_ref),
                    ("revocation_ids", "=", False),
                ],
                order="id asc",
            )
        )
        return self._revoke_attribution_assertions(
            assertions,
            authority_key,
            authority_ref,
            revocation_ref,
            reason=reason,
        )

    @api.model
    def _link_event_lead(self, event, lead):
        event = event.exists()
        if getattr(event, "_name", "") != "marketing.business.event" or len(event) != 1:
            raise ValidationError(_("A single valid marketing event is required."))
        company = self._company_for_lead(lead)
        if event.company_id != company:
            raise ValidationError(
                _("Marketing events and CRM leads cannot cross companies.")
            )
        lock_key = "marketing_crm_event:%s:%s:%s" % (company.id, event.id, lead.id)
        acquire_advisory_xact_lock(self.env.cr, lock_key)
        Link = self.env["marketing.business.event.crm.link"].sudo()
        link = Link.search(
            [
                ("company_id", "=", company.id),
                ("event_id", "=", event.id),
                ("lead_id", "=", lead.id),
            ],
            limit=1,
        )
        if link:
            return link
        return (
            Link.with_company(company)
            .with_context(marketing_crm_link_write_token=MARKETING_CRM_LINK_WRITE_TOKEN)
            .create(
                {
                    "company_id": company.id,
                    "event_id": event.id,
                    "lead_id": lead.id,
                    **self._lead_snapshot_values(lead),
                }
            )
        )

    @api.model
    def _prepare_lead_merge(self, leads):
        """Capture immutable source evidence before native CRM deletes merge tails."""

        leads = leads.exists()
        if getattr(leads, "_name", "") != "crm.lead" or len(leads) <= 1:
            return []
        self.env.cr.execute(
            "SELECT id FROM crm_lead WHERE id IN %s ORDER BY id FOR UPDATE",
            [tuple(leads.ids)],
        )
        leads.invalidate_recordset(["company_id", "marketing_event_company_id", "name"])
        ordered = leads._sort_by_confidence_level(reverse=True)
        target = ordered[:1]
        target_company = self._company_for_lead(target)
        payloads = []
        for source in ordered[1:]:
            source_company = self._company_for_lead(source)
            if source_company != target_company:
                raise ValidationError(
                    _(
                        "CRM leads with marketing evidence cannot be merged across companies."
                    )
                )
            active_assertions = (
                self.env["marketing.attribution.crm.link"]
                .sudo()
                .search(
                    [
                        ("lead_id", "=", source.id),
                        ("company_id", "=", source_company.id),
                        ("revocation_ids", "=", False),
                        "|",
                        ("derived_from_assertion_id", "=", False),
                        ("derived_from_assertion_id.revocation_ids", "=", False),
                    ],
                    order="id",
                )
            )
            event_links = (
                self.env["marketing.business.event.crm.link"]
                .sudo()
                .search(
                    [
                        ("lead_id", "=", source.id),
                        ("company_id", "=", source_company.id),
                    ],
                    order="id",
                )
            )
            payloads.append(
                {
                    "company_id": source_company.id,
                    "source": self._lead_snapshot_values(source),
                    "target": self._lead_snapshot_values(target),
                    "assertions": [
                        (
                            assertion.id,
                            assertion.touchpoint_id.id,
                            assertion.authority_key,
                            assertion.authority_ref,
                            assertion.source_ref,
                            (assertion.derived_from_assertion_id.id or assertion.id),
                        )
                        for assertion in active_assertions
                    ],
                    "event_ids": event_links.mapped("event_id").ids,
                }
            )
        return payloads

    @api.model
    def _record_lead_merge(self, target, payloads):
        """Append equivalences and new projections without rewriting old evidence."""

        target = target.exists()
        if len(target) != 1:
            raise ValidationError(_("The merged CRM lead is unavailable."))
        Equivalence = self.env["marketing.crm.lead.equivalence"].sudo()
        for payload in payloads:
            company = self.env["res.company"].browse(payload["company_id"]).exists()
            if not company or self._company_for_lead(target) != company:
                raise ValidationError(
                    _("CRM lead merge evidence cannot cross companies.")
                )
            source = payload["source"]
            expected_target = payload["target"]
            current_target = self._lead_snapshot_values(target)
            if current_target["lead_res_id"] != expected_target["lead_res_id"]:
                raise ValidationError(_("The CRM merge target changed unexpectedly."))
            merge_ref = "crm.lead:%s:merged-into:%s" % (
                source["lead_res_id"],
                current_target["lead_res_id"],
            )
            equivalence = Equivalence.search(
                [
                    ("company_id", "=", company.id),
                    ("source_lead_model", "=", source["lead_model"]),
                    ("source_lead_res_id", "=", source["lead_res_id"]),
                ],
                limit=1,
            )
            values = {
                "company_id": company.id,
                "merge_ref": merge_ref,
                "source_lead_model": source["lead_model"],
                "source_lead_res_id": source["lead_res_id"],
                "source_lead_display_ref": source["lead_display_ref"],
                "target_lead_id": target.id,
                "target_lead_model": current_target["lead_model"],
                "target_lead_res_id": current_target["lead_res_id"],
                "target_lead_display_ref": current_target["lead_display_ref"],
            }
            if equivalence:
                persisted = {
                    "company_id": equivalence.company_id.id,
                    "merge_ref": equivalence.merge_ref,
                    "source_lead_model": equivalence.source_lead_model,
                    "source_lead_res_id": equivalence.source_lead_res_id,
                    "source_lead_display_ref": equivalence.source_lead_display_ref,
                    "target_lead_id": equivalence.target_lead_id.id,
                    "target_lead_model": equivalence.target_lead_model,
                    "target_lead_res_id": equivalence.target_lead_res_id,
                    "target_lead_display_ref": equivalence.target_lead_display_ref,
                }
                if persisted != values:
                    raise ValidationError(
                        _("The CRM lead was already merged into another target.")
                    )
            else:
                (
                    Equivalence.with_company(company)
                    .with_context(
                        marketing_crm_link_write_token=MARKETING_CRM_LINK_WRITE_TOKEN
                    )
                    .create(values)
                )
            for (
                assertion_id,
                touchpoint_id,
                authority_key,
                authority_ref,
                source_ref,
                root_assertion_id,
            ) in payload["assertions"]:
                material = "%s\x1f%s" % (merge_ref, assertion_id)
                self._link_touchpoint_lead(
                    self.env["marketing.attribution.touchpoint"].browse(touchpoint_id),
                    target,
                    source_ref=source_ref,
                    authority_key=authority_key,
                    authority_ref=authority_ref,
                    assertion_ref="merge:%s"
                    % hashlib.sha256(material.encode("utf-8")).hexdigest(),
                    derived_from_assertion=self.env[
                        "marketing.attribution.crm.link"
                    ].browse(root_assertion_id),
                )
            for event in self.env["marketing.business.event"].browse(
                payload["event_ids"]
            ):
                self._link_event_lead(event, target)
        return True

    @api.model
    def _ingest_lead_event(
        self,
        lead,
        event_type,
        occurrence_ref,
        occurred_at=None,
        extensions=None,
    ):
        company = self._company_for_lead(lead)
        occurrence_ref = (occurrence_ref or "").strip()
        if not occurrence_ref or len(occurrence_ref) > 512:
            raise ValidationError(_("The CRM event occurrence reference is invalid."))
        event_key = "crm.lead:%s:%s:%s" % (lead.id, occurrence_ref, event_type)
        dto = MarketingBusinessEventDTO(
            event_class="lifecycle",
            event_type=event_type,
            source_system="odoo.crm",
            source_model="crm.lead",
            source_res_id=lead.id,
            source_occurrence_ref=occurrence_ref,
            source_evidence_ref=occurrence_ref,
            business_event_key=event_key,
            occurred_at=occurred_at or fields.Datetime.now(),
            evidence_level="first_party",
            extensions=extensions or {},
        )
        result = (
            self.env["marketing.business.event.service"]
            .with_company(company)
            ._ingest_event(company, dto)
        )
        # The projection is internal. CRM salespeople need not receive direct
        # access to the provider-neutral event ledger merely to create a lead.
        event = self.env["marketing.business.event"].sudo().browse(result.event_id)
        self._link_event_lead(event, lead)
        return event

    @api.model
    def _existing_lead_event(self, lead, event_type, occurrence_ref):
        company = self._company_for_lead(lead)
        event_key = "crm.lead:%s:%s:%s" % (lead.id, occurrence_ref, event_type)
        return (
            self.env["marketing.business.event"]
            .sudo()
            .search(
                [
                    ("company_id", "=", company.id),
                    ("source_system", "=", "odoo.crm"),
                    ("business_event_key", "=", event_key),
                ],
                limit=1,
            )
        )

    @api.model
    def _known_transition_sequence(self, lead, event_type, occurrence_ref):
        event = self._existing_lead_event(lead, event_type, occurrence_ref)
        if not event or not event.snapshot_json:
            return 0
        return int(
            event.snapshot_json.get("extensions", {}).get("crm.transition_sequence")
            or 0
        )

    @api.model
    def _emit_stage_events(
        self,
        lead,
        old_stage,
        new_stage,
        occurrence_ref,
        occurred_at=None,
        sequence=0,
        tracking_watermark=None,
    ):
        if old_stage == new_stage:
            return self.env["marketing.business.event"]
        sequence = sequence or self._known_transition_sequence(
            lead, "lead_stage_changed", occurrence_ref
        )
        extensions = {
            "crm.old_stage_id": old_stage.id or 0,
            "crm.new_stage_id": new_stage.id or 0,
        }
        if sequence:
            extensions["crm.transition_sequence"] = sequence
        if tracking_watermark is not None:
            extensions["crm.tracking_watermark"] = tracking_watermark
        events = self._ingest_lead_event(
            lead,
            "lead_stage_changed",
            occurrence_ref,
            occurred_at=occurred_at,
            extensions=extensions,
        )
        if new_stage.is_won:
            events |= self._ingest_lead_event(
                lead,
                "won",
                occurrence_ref,
                occurred_at=occurred_at,
                extensions=extensions,
            )
        if new_stage.marketing_semantic == "qualified":
            events |= self._ingest_lead_event(
                lead,
                "qualified",
                occurrence_ref,
                occurred_at=occurred_at,
                extensions=extensions,
            )
        return events

    @api.model
    def _emit_lost_event(
        self,
        lead,
        occurrence_ref,
        occurred_at=None,
        sequence=0,
        lost_reason=None,
        tracking_watermark=None,
    ):
        sequence = sequence or self._known_transition_sequence(
            lead, "lost", occurrence_ref
        )
        extensions = {
            "crm.lost_reason_id": (lost_reason or lead.lost_reason_id).id or 0
        }
        if sequence:
            extensions["crm.transition_sequence"] = sequence
        if tracking_watermark is not None:
            extensions["crm.tracking_watermark"] = tracking_watermark
        return self._ingest_lead_event(
            lead,
            "lost",
            occurrence_ref,
            occurred_at=occurred_at,
            extensions=extensions,
        )

    @api.model
    def _backfill_lead_events(self, lead, after_tracking_id=0, limit=200):
        company = self._company_for_lead(lead)
        after_tracking_id = max(int(after_tracking_id or 0), 0)
        limit = min(max(int(limit or 200), 1), 1000)
        created = self._existing_lead_event(lead, "lead_created", "created")
        if not created:
            self._ingest_lead_event(
                lead,
                "lead_created",
                "created",
                occurred_at=lead.create_date or fields.Datetime.now(),
                extensions={"crm.lead_type": lead.type},
            )
        events = (
            self.env["marketing.business.event"]
            .sudo()
            .search(
                [
                    ("company_id", "=", company.id),
                    ("source_system", "=", "odoo.crm"),
                    ("source_model", "=", "crm.lead"),
                    ("source_res_id", "=", lead.id),
                ]
            )
        )
        watermarks = [
            event.snapshot_json["extensions"]["crm.tracking_watermark"]
            for event in events
            if "crm.tracking_watermark" in event.snapshot_json.get("extensions", {})
        ]
        # Odoo creates chatter tracking at precommit, after the live sequence
        # events already exist. Only history preceding live capture needs
        # importing; later chatter rows describe those same occurrences.
        historical_domain = [("id", "<=", min(watermarks))] if watermarks else []
        tracking_values = (
            self.env["mail.tracking.value"]
            .sudo()
            .search(
                [
                    ("id", ">", after_tracking_id),
                    ("mail_message_id.model", "=", "crm.lead"),
                    ("mail_message_id.res_id", "=", lead.id),
                    ("field.name", "in", ["stage_id", "active"]),
                ]
                + historical_domain,
                order="id asc",
                limit=limit,
            )
        )
        for tracking in tracking_values:
            occurrence_ref = "tracking:%s" % tracking.id
            occurred_at = tracking.mail_message_id.date or fields.Datetime.now()
            if tracking.field.name == "stage_id":
                old_stage = (
                    self.env["crm.stage"].browse(tracking.old_value_integer).exists()
                )
                new_stage = (
                    self.env["crm.stage"].browse(tracking.new_value_integer).exists()
                )
                self._emit_stage_events(
                    lead, old_stage, new_stage, occurrence_ref, occurred_at=occurred_at
                )
            elif tracking.old_value_integer and not tracking.new_value_integer:
                self._emit_lost_event(
                    lead,
                    occurrence_ref,
                    occurred_at=occurred_at,
                    lost_reason=lead.lost_reason_id,
                )
        return {
            "processed": len(tracking_values),
            "last_tracking_id": tracking_values[-1:].id or after_tracking_id,
            "has_more": len(tracking_values) == limit,
            "company_id": company.id,
        }

    @api.model
    def _lead_action(self, leads):
        leads = leads.exists()
        leads.check_access_rights("read")
        leads.check_access_rule("read")
        action = self.env["ir.actions.actions"]._for_xml_id("crm.crm_lead_all_leads")
        # The native action carries ``res_id: 0``.  Remove it before adapting the
        # action so empty and multi-record drilldowns always open a list rather
        # than a synthetic form target.
        action.pop("res_id", None)
        action.update(
            {"domain": [("id", "in", leads.ids)], "context": {"create": False}}
        )
        if len(leads) == 1:
            action.update(
                {"res_id": leads.id, "view_mode": "form", "views": [(False, "form")]}
            )
        return action

"""Symmetric commercial admission. No optional Meta model inheritance."""

import datetime
from contextlib import contextmanager

from odoo import api, models
from odoo.exceptions import AccessError, MissingError, ValidationError

from odoo.addons.contact_center_crm.models.auto_origin import lead_is_open
from odoo.addons.contact_center_crm.models.intake_policy import COMPANY_GATE_TOKEN
from odoo.addons.marketing_center_base.models.crm.dedup_policy import (
    CrmDedupUnavailable,
)


class Admission(models.AbstractModel):
    _inherit = "marketing.crm.service"

    @api.model
    def _crm_cross_source_capable(self):
        return True

    @api.model
    @contextmanager
    def _crm_cross_source_gate(self, company, force=False):
        # Also fence flag=False jobs, so activation cannot race a legacy create.
        gate = self.env["contact.center.crm.intake.gate"]._acquire_company(company)
        yield self.with_context(
            crm_company_gate=COMPANY_GATE_TOKEN,
            crm_company_gate_id=gate.id,
            crm_company_gate_company=company.id,
        )

    @api.model
    def _admission_gate(self, company):
        if (
            self.env.context.get("crm_company_gate") is not COMPANY_GATE_TOKEN
            or self.env.context.get("crm_company_gate_company") != company.id
        ):
            raise CrmDedupUnavailable("bridge_error")
        return (
            self.env["contact.center.crm.intake.gate"]
            .sudo()
            .search(
                [
                    ("company_id", "=", company.id),
                ],
                limit=1,
            )
            .with_context(crm_company_gate=COMPANY_GATE_TOKEN)
        )

    @api.model
    def _crm_cross_source_check_gate(self, company):
        if company.sudo().crm_cross_source_dedup_enabled:
            self._admission_gate(company)
        return True

    @api.model
    def _admission_receipts(self, company, keys, occurred_at, exclude=None):
        """Search the occurrence window, even when the CRM target was deleted."""
        delta = datetime.timedelta(hours=company.sudo().crm_cross_source_window_hours)
        result = []
        for model, prefix, at_field, target_field, state_field in (
            (
                "contact.center.channel.binding",
                "crm_comparison_",
                "crm_comparison_source_at",
                "crm_intake_lead_snapshot",
                "crm_intake_state",
            ),
            (
                "marketing.center.meta.crm.projection",
                "comparison_",
                "comparison_source_at",
                "lead_res_id",
                "state",
            ),
        ):
            if model not in self.env.registry:
                continue
            domain = [
                ("company_id", "=", company.id),
                (prefix + "erased_at", "=", False),
                (at_field, ">=", occurred_at - delta),
                (at_field, "<=", occurred_at + delta),
            ]
            if keys["variant"]:
                domain += [
                    "|",
                    (prefix + "exact", "=", keys["exact"]),
                    (prefix + "variant", "=", keys["variant"]),
                ]
            else:
                domain += [(prefix + "exact", "=", keys["exact"])]
            if exclude and exclude._name == model:
                domain += [("id", "!=", exclude.id)]
            rows = (
                self.env[model]
                .sudo()
                .with_context(active_test=False)
                .search(domain, order="id", limit=5)
            )
            for row in rows:
                # Any undecided/decided matching demand is significant. Dismissal
                # never becomes authorization to recreate it through another path.
                result.append(
                    {
                        "record": row,
                        "target": row[target_field],
                        "state": row[state_field],
                        "at": row[at_field],
                        "weak": row[prefix + "exact"] != keys["exact"],
                        "overflow": len(rows) > 4,
                    }
                )
        return result

    @api.model
    def _admission_origins(self, receipt):
        """Return only proved acquisition facts for comparison, never UI data."""
        if receipt._name == "marketing.center.meta.crm.projection":
            submission = receipt.submission_id.sudo()
            if submission.touchpoint_id.privacy_erased_at:
                return {"unavailable": True, "own": True, "ads": set()}
            return {
                "unavailable": False,
                "own": True,
                "ads": {submission.provider_ad_id} - {False, ""},
            }
        touchpoints = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search(
                [
                    ("channel_binding_id", "=", receipt.id),
                ],
                limit=101,
            )
        )
        ads, own, unavailable = set(), False, len(touchpoints) > 100
        for touchpoint in touchpoints:
            for identifier in touchpoint.identifier_ids:
                if (
                    identifier.namespace in {"meta.ad_id", "meta.source_id"}
                    and identifier.role == "ad_source"
                    and touchpoint.evidence_level == "provider_asserted"
                ):
                    ads.add(identifier.value)
                    own = True
            own = own or bool(touchpoint.utm_source or touchpoint.utm_campaign)
        # Site acquisition belongs to the conversation too. Unknown compatibility
        # must go to review, including acquisition not mapped to a catalog yet.
        if "marketing.website.whatsapp.match" in self.env.registry:
            matches = (
                self.env["marketing.website.whatsapp.match"]
                .sudo()
                .search(
                    [
                        ("channel_id", "=", receipt.channel_id.id),
                        ("state", "in", ["reference", "confirmed"]),
                    ],
                    limit=101,
                )
            )
            unavailable = unavailable or len(matches) > 100
            for match in matches:
                handoff = match.handoff_id
                own = True
                unavailable = unavailable or bool(handoff.erased_at)
                point = handoff.journey_touchpoint_id
                if point:
                    assets = point.asset_refs_json or {}
                    ads.update({assets.get("meta.ad_id")} - {False, None, ""})
        return {"unavailable": unavailable, "own": own, "ads": ads}

    @api.model
    def _admission_evaluate(
        self, company, phone, occurred_at, receipt, actor, native_candidates=None
    ):
        gate = self._admission_gate(company)
        country = company.sudo().country_id.code or "BR"
        keys = gate._comparison_keys(phone, country)
        decision = {
            "decision": "create",
            "lead_id": False,
            "candidate_ids": [],
            "reason": False,
            "keys": keys,
        }
        if not keys["exact"]:
            decision.update(identity="unverified")
            return decision
        exact, weak = gate._phone_candidates(phone, country)
        receipts = self._admission_receipts(company, keys, occurred_at, exclude=receipt)
        targets = {item["target"] for item in receipts if item["target"]}
        # Historical closed businesses are not new-demand identity receipts.
        # Native explicit partner/link candidates remain authoritative for intake.
        candidates = (
            exact.filtered(lead_is_open) | exact.browse(sorted(targets)).exists()
        )
        if native_candidates is not None:
            candidates |= exact.browse(native_candidates.ids).exists()
        weak = weak.filtered(lead_is_open)
        decision["candidate_ids"] = (candidates | weak).ids

        def review(reason):
            decision.update(decision="review", reason=reason)
            return decision

        if weak or any(item["weak"] for item in receipts):
            return review("phone_variant_review")
        if any(not item["target"] for item in receipts):
            return review("business_scope_review")
        if targets - set(candidates.ids):
            return review("receipt_without_target")
        if (
            len(candidates) > 1
            or len(targets) > 1
            or any(item["overflow"] for item in receipts)
        ):
            return review("ambiguous")
        if not candidates:
            return decision
        lead = candidates
        if lead.company_id != company:
            return review("company_review")
        visible = actor["crm.lead"].with_context(active_test=False).browse(lead.id)
        try:
            visible.check_access_rights("read")
            visible.check_access_rule("read")
        except AccessError:
            decision["candidate_ids"] = []
            return review("inaccessible")
        if not lead_is_open(lead):
            return review("closed_business_review")
        if receipt._name == "contact.center.channel.binding":
            # Preserve the established WhatsApp policy: one open business is
            # linked as context. This grants no Meta attribution authority.
            decision.update(decision="reuse", lead_id=lead.id, identity="exact_phone")
            return decision
        if not self._admission_can_credit_meta(
            company, occurred_at, receipt, receipts, lead
        ):
            return review("business_scope_review")
        decision.update(decision="reuse", lead_id=lead.id, identity="exact_phone")
        return decision

    @api.model
    def _admission_can_credit_meta(self, company, occurred_at, receipt, receipts, lead):
        if not receipts or {item["target"] for item in receipts} != {lead.id}:
            return False
        cutoff = company.sudo().crm_cross_source_enabled_at
        if (
            not cutoff
            or occurred_at < cutoff
            or any(item["at"] < cutoff for item in receipts)
        ):
            return False

        def created_in_cohort(item):
            source = item["record"]
            if source._name == "contact.center.channel.binding":
                return (
                    source.crm_intake_state == "created"
                    and source.id > company.crm_cross_source_binding_watermark
                    and source.crm_intake_admitted_at >= cutoff
                )
            return (
                source.admission_decision == "created"
                and source.id > company.crm_cross_source_projection_watermark
                and source.submission_id.id
                > company.crm_cross_source_submission_watermark
            )

        if (
            not lead.create_date
            or lead.create_date < cutoff
            or not any(created_in_cohort(item) for item in receipts)
        ):
            return False
        links = (
            self.env["contact.center.crm.conversation.link"]
            .sudo()
            .search(
                [
                    ("lead_id", "=", lead.id),
                    ("state", "=", "active"),
                ]
            )
        )
        if lead.marketing_utm_manual or any(
            link.scope_decision_mode == "human" or link.writer == "manual"
            for link in links
        ):
            return False
        if not self._admission_compatible_origins(receipt, receipts):
            return False
        return True

    @api.model
    def _admission_compatible_origins(self, receipt, receipts):
        incoming = self._admission_origins(receipt)
        for item in receipts:
            previous = self._admission_origins(item["record"])
            if incoming["unavailable"] or previous["unavailable"]:
                return False
            if (
                incoming["ads"]
                and previous["ads"]
                and incoming["ads"] != previous["ads"]
            ):
                return False
            if (
                incoming["own"]
                and previous["own"]
                and not (incoming["ads"] and incoming["ads"] == previous["ads"])
            ):
                # Missing ad IDs are allowed for authenticated Meta on an
                # otherwise origin-less new WhatsApp demand, not arbitrary
                # competing acquisition evidence.
                return False
        return True

    @api.model
    def _crm_cross_source_admit_meta(self, projection, values):
        company = projection.company_id.sudo()
        if (
            not company.crm_cross_source_dedup_enabled
            and not projection.technical_hold_reason
        ):
            return super()._crm_cross_source_admit_meta(projection, values)
        if not company.crm_cross_source_dedup_enabled:
            raise CrmDedupUnavailable("bridge_error")
        self._admission_gate(company)
        if (
            projection.comparison_erased_at
            or projection.submission_id.touchpoint_id.privacy_erased_at
            or not projection.submission_id.provider_created_at
        ):
            return {
                "decision": "review",
                "lead_id": False,
                "candidate_ids": [],
                "reason": "business_scope_review",
                "keys": {"exact": False, "variant": False, "version": 1},
            }
        reviewer = company.crm_cross_source_reviewer_id
        actor = api.Environment(
            self.env.cr, reviewer.id, {"allowed_company_ids": company.ids}
        )
        result = self._admission_evaluate(
            company,
            values.get("phone"),
            projection.submission_id.provider_created_at,
            projection,
            actor,
        )
        keys = result["keys"]
        projection._internal_write(
            {
                "comparison_exact": keys["exact"],
                "comparison_variant": keys["variant"],
                "comparison_version": keys["version"],
                "comparison_source_at": projection.submission_id.provider_created_at,
                "comparison_policy_revision": company.crm_cross_source_revision,
            }
        )
        return result

    @api.model
    def _crm_cross_source_intake_counts(self, company):
        domain = [
            ("company_id", "=", company.id),
            ("id", ">", company.sudo().crm_cross_source_binding_watermark),
            (
                "crm_intake_admitted_at",
                ">=",
                company.sudo().crm_cross_source_enabled_at,
            ),
            ("crm_intake_state", "=", "review"),
        ]
        pending = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .search(domain, order="crm_intake_admitted_at, id")
        )
        manual_links = (
            self.env["contact.center.crm.conversation.link"]
            .sudo()
            .search(
                [
                    ("company_id", "=", company.id),
                    ("channel_id", "in", pending.mapped("channel_id").ids),
                    ("state", "=", "active"),
                    ("writer", "=", "manual"),
                ]
            )
        )
        for link in manual_links:
            binding = pending.filtered(lambda row: row.channel_id == link.channel_id)
            if not binding or not link.linked_by_id.active:
                continue
            actor = api.Environment(
                self.env.cr,
                link.linked_by_id.id,
                {"allowed_company_ids": company.ids},
            )
            try:
                candidates = binding._crm_intake_review_candidates(actor)
                native = actor["crm.lead"].browse(link.lead_id.id)
                native.check_access_rights("read")
                native.check_access_rule("read")
            except (AccessError, MissingError, ValidationError):
                continue
            if link.lead_id.id in candidates.ids:
                pending -= binding
        return {
            "count": len(pending),
            "oldest": pending[:1].crm_intake_admitted_at if pending else False,
        }

    @api.model
    def _crm_cross_source_review_candidates(self, projection, values):
        gate = (
            self.env["contact.center.crm.intake.gate"]
            .sudo()
            .search([("company_id", "=", projection.company_id.id)], limit=1)
        )
        if not gate:
            return self.env["crm.lead"].browse()
        exact, weak = gate._phone_candidates(
            values.get("phone"),
            projection.company_id.country_id.code or "BR",
            review_only=True,
            review_actor=self.env,
        )
        return (exact | weak).filtered(
            lambda lead: lead.company_id == projection.company_id
        )


class Binding(models.Model):
    _inherit = "contact.center.channel.binding"

    def _crm_intake_prepare_comparison(self, actor):
        super()._crm_intake_prepare_comparison(actor)
        company = self.company_id.sudo()
        if (
            not company.crm_cross_source_dedup_enabled
            or self.crm_comparison_erased_at
            or self.crm_comparison_exact
        ):
            return
        service = self.env["marketing.crm.service"]
        numbers = actor["contact.center.ui.api"]._crm_phone_numbers(
            actor["mail.channel"].browse(self.channel_id.id)
        )
        if len(numbers) != 1:
            return
        keys = service._admission_gate(company)._comparison_keys(
            "+" + next(iter(numbers))
        )
        first = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [
                    ("channel_binding_id", "=", self.id),
                    ("direction", "=", "inbound"),
                    ("origin", "=", "provider"),
                ],
                order="id",
                limit=1,
            )
        )
        self._crm_intake_write(
            {
                "crm_comparison_exact": keys["exact"],
                "crm_comparison_variant": keys["variant"],
                "crm_comparison_version": keys["version"],
                "crm_comparison_source_at": first.message_id.date
                or self.crm_intake_source_at,
                "crm_comparison_policy_revision": company.crm_cross_source_revision,
            }
        )

    def _crm_intake_candidates(self, actor):
        candidates, partner, reason = super()._crm_intake_candidates(actor)
        if not self.company_id.sudo().crm_cross_source_dedup_enabled or reason in {
            "ambiguous",
            "company_review",
            "inaccessible",
            "identity_unavailable",
        }:
            return candidates, partner, reason
        if not self.crm_comparison_exact or self.crm_comparison_erased_at:
            return candidates, partner, "identity_unavailable"
        numbers = actor["contact.center.ui.api"]._crm_phone_numbers(
            actor["mail.channel"].browse(self.channel_id.id)
        )
        result = self.env["marketing.crm.service"]._admission_evaluate(
            self.company_id,
            "+" + next(iter(numbers)),
            self.crm_comparison_source_at,
            self,
            actor,
            native_candidates=candidates,
        )
        if result["decision"] == "review":
            return (
                actor["crm.lead"].browse(result["candidate_ids"]),
                partner,
                result["reason"],
            )
        return actor["crm.lead"].browse(result["lead_id"]), partner, reason

    def _crm_intake_review_candidates(self, actor):
        if not self.sudo().crm_comparison_policy_revision:
            return super()._crm_intake_review_candidates(actor)
        numbers = actor["contact.center.ui.api"]._crm_phone_numbers(
            actor["mail.channel"].browse(self.channel_id.id)
        )
        if len(numbers) != 1:
            return actor["crm.lead"].browse()
        gate = (
            self.env["contact.center.crm.intake.gate"]
            .sudo()
            .search([("company_id", "=", self.company_id.id)], limit=1)
        )
        exact, weak = gate._phone_candidates(
            "+" + next(iter(numbers)),
            self.company_id.country_id.code or "BR",
            review_only=True,
            review_actor=actor,
        )
        native = super()._crm_intake_review_candidates(actor)
        return (exact | weak | exact.browse(native.ids)).filtered(
            lambda lead: lead.company_id == self.company_id
        )

    def _crm_intake_review_match(self, lead, actor):
        if not lead or not self.sudo().crm_comparison_policy_revision:
            return super()._crm_intake_review_match(lead, actor)
        numbers = actor["contact.center.ui.api"]._crm_phone_numbers(
            actor["mail.channel"].browse(self.channel_id.id)
        )
        if len(numbers) != 1:
            return "business"
        gate = (
            self.env["contact.center.crm.intake.gate"]
            .sudo()
            .search([("company_id", "=", self.company_id.id)], limit=1)
        )
        exact, weak = gate._phone_candidates(
            "+" + next(iter(numbers)),
            self.company_id.country_id.code or "BR",
            review_only=True,
            review_actor=actor,
        )
        return (
            "br_pair"
            if lead.id in weak.ids
            else "exact_phone"
            if lead.id in exact.ids
            else "business"
        )

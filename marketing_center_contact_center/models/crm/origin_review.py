"""Minimal document-scoped origin projection and review adapters."""

from odoo import fields, models
from odoo.exceptions import AccessError, ValidationError


class Lead(models.Model):
    _inherit = "crm.lead"

    def _marketing_scope_review_requires_valid_support(self):
        self.ensure_one()
        return (
            bool(self._conversation_links().filtered("automatic_lineage"))
            or super()._marketing_scope_review_requires_valid_support()
        )

    def _journey_minimal_origin(self, point, link, credit_at):
        self.ensure_one()
        scope = link._crm_origin_evidence_scope(credit_at, point.canonical_key)
        result = {
            "type": point.touchpoint_type,
            "at": fields.Datetime.to_string(point.occurred_at),
            "scope": scope,
            "review_reason": link._crm_origin_pending_reason(credit_at) or False,
            "evidence_key": point.canonical_key,
            "can_review": False,
            "campaign_name": False,
            "ad_name": False,
            "source_name": False,
            "medium_name": False,
        }
        try:
            self.check_access_rights("write")
            self.check_access_rule("write")
            self.env["contact.center.ui.api"]._crm_channel(
                link.channel_id.id, mutate=True
            )
            result["can_review"] = link.scope_state == "confirmed"
        except (AccessError, ValidationError):
            result["can_review"] = False
        if point.privacy_erased_at or point.consent_state == "denied":
            result.update(
                scope="ineligible",
                can_review=False,
                review_reason="privacy_unavailable",
            )
            return result
        classification = (
            self.env["marketing.native.utm.service"].sudo()._resolve_touchpoint(point)
        )
        if classification.get("entity_id"):
            result["campaign_name"] = (
                self.env["marketing.center.external.entity"]
                .sudo()
                .browse(classification["entity_id"])
                .name
            )
        for key, model, target in (
            ("campaign_id", "utm.campaign", "campaign_name"),
            ("utm_source_id", "utm.source", "source_name"),
            ("medium_id", "utm.medium", "medium_name"),
        ):
            if classification.get(key):
                native = self.env[model].sudo().browse(classification[key])
                result[target] = (
                    (native.title or native.name)
                    if model == "utm.campaign"
                    else native.name
                )
        resolution = (
            self.env["marketing.attribution.asset.resolution"]
            .sudo()
            .search(
                [
                    ("company_id", "=", point.company_id.id),
                    ("canonical_key", "=", point.canonical_key),
                    ("state", "=", "resolved"),
                    ("mapped_entity_type", "=", "ad"),
                ],
                order="id desc",
                limit=1,
            )
        )
        if resolution.entity_id:
            result["ad_name"] = resolution.entity_id.name
        return result

    def _journey_origin_credit(self, channel, link, evidence_key):
        mapped = (
            self.env["marketing.attribution.contact.center.link"]
            .sudo()
            .search(
                [
                    ("company_id", "=", link.company_id.id),
                    (
                        "source_touchpoint_id.channel_binding_id.channel_id",
                        "=",
                        channel.id,
                    ),
                    ("marketing_touchpoint_id.canonical_key", "=", evidence_key),
                ],
                order="id desc",
                limit=1,
            )
        )
        if mapped:
            point = mapped.marketing_touchpoint_id
            if not point.privacy_erased_at and point.consent_state != "denied":
                return point.occurred_at
            return None
        return super()._journey_origin_credit(channel, link, evidence_key)

    def _marketing_scope_review_allows_classification(self, eligible):
        self.ensure_one()
        active = self._conversation_links()
        if self.marketing_cc_orphan_review or active.filtered(
            lambda row: row.scope_state != "confirmed"
            or row.marketing_scope_pending
            or (
                "website_journey_pending" in row._fields and row.website_journey_pending
            )
        ):
            return False
        if "marketing.website.whatsapp.match" in self.env.registry and self.env[
            "marketing.website.whatsapp.match"
        ].sudo().search_count(
            [
                ("company_id", "=", self.company_id.id),
                ("channel_id", "in", active.channel_id.ids),
                ("journey_pending", "=", True),
            ]
        ):
            return False
        all_links = (
            self.env["marketing.attribution.crm.effective.link"]
            .sudo()
            .search(
                [("lead_id", "=", self.id), ("company_id", "=", self.company_id.id)]
            )
        )
        pending = all_links._scope_partition()["pending"]
        if not pending:
            return super()._marketing_scope_review_allows_classification(eligible)
        if not active or not all(active.mapped("automatic_lineage")):
            return False
        for point in pending:
            assertions = (
                self.env["marketing.attribution.crm.link"]
                .sudo()
                .search(
                    [
                        ("lead_id", "=", self.id),
                        ("company_id", "=", self.company_id.id),
                        ("canonical_key", "=", point.canonical_key),
                        ("revocation_ids", "=", False),
                    ]
                )
            )
            if any(
                row.authority_key
                not in {"contact_center.conversation", "website.whatsapp"}
                for row in assertions
            ):
                return False
        return True


class ConversationLink(models.Model):
    _inherit = "contact.center.crm.conversation.link"

    def _crm_origin_decision_changed(self, decision):
        result = super()._crm_origin_decision_changed(decision)
        self.env[
            "marketing.contact.center.crm.service"
        ].sudo()._enqueue_conversation_links(self)
        if hasattr(self, "_enqueue_website_journey"):
            self._enqueue_website_journey()
        self.mapped("lead_id")._enqueue_native_utm()
        return result


class Touchpoint(models.Model):
    _inherit = "marketing.attribution.touchpoint"

    def _erase_private_values(self, *, token, now):
        result = super()._erase_private_values(token=token, now=now)
        for point in self:
            mappings = (
                self.env["marketing.attribution.contact.center.link"]
                .sudo()
                .search(
                    [
                        ("company_id", "=", point.company_id.id),
                        (
                            "marketing_touchpoint_id.canonical_key",
                            "=",
                            point.canonical_key,
                        ),
                    ]
                )
            )
            mappings.mapped(
                "source_touchpoint_id.channel_binding_id"
            )._crm_erase_comparison()
        return result

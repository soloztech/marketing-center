"""Business eligibility without mutating immutable acquisition evidence."""
from odoo import api, fields, models


class EffectiveLink(models.Model):
    _inherit = "marketing.attribution.crm.effective.link"

    def _assertion_scope(self, assertion):
        self.ensure_one()
        if assertion.authority_key == "contact_center.case":
            return "pending"
        if assertion.authority_key != "contact_center.conversation":
            return super()._assertion_scope(assertion)
        ref = assertion.authority_ref or ""
        if not ref.isascii() or not ref.isdigit() or int(ref) <= 0:
            return "pending"
        row = (
            self.env["contact.center.crm.conversation.link"]
            .sudo()
            .search(
                [
                    ("channel_id", "=", int(ref)),
                    ("lead_id", "=", self.lead_id.id),
                    ("company_id", "=", self.company_id.id),
                    ("state", "=", "active"),
                ],
                limit=1,
            )
        )
        if not row or row.scope_state != "confirmed":
            return "pending"
        return (
            "eligible"
            if row._scope_contains(self.touchpoint_id.sudo().occurred_at)
            else "ineligible"
        )


class CrmLead(models.Model):
    _inherit = "crm.lead"

    marketing_cc_orphan_review = fields.Boolean(readonly=True, copy=False)

    def _native_utm_protected_fields(self):
        return super()._native_utm_protected_fields() | {"marketing_cc_orphan_review"}

    def _marketing_scope_review_pending(self):
        self.ensure_one()
        return (
            bool(
                self.marketing_cc_orphan_review
                or self._conversation_links().filtered(
                    lambda row: row.scope_state == "review"
                    or row.marketing_scope_pending
                )
            )
            or super()._marketing_scope_review_pending()
        )

    def _journey_scope_review_pending(self, pending):
        self.ensure_one()
        if not self.marketing_utm_manual:
            return bool(pending or self._marketing_scope_review_pending())
        active = self._conversation_links()
        if active.filtered(
            lambda row: row.scope_state == "review" or row.marketing_scope_pending
        ):
            return True
        # Manual UTM ownership resolves orphan classification. It does not
        # confirm a live business period or make its acquisition eligible.
        live_ids = set(
            active.filtered(
                lambda row: row.scope_state in ("legacy", "context")
            ).channel_id.ids
        )
        if not pending or not live_ids:
            return False
        support = (
            self.env["marketing.attribution.crm.link"]
            .sudo()
            .search(
                [
                    ("lead_id", "=", self.id),
                    ("company_id", "in", pending.company_id.ids),
                    ("canonical_key", "in", pending.mapped("canonical_key")),
                    ("authority_key", "=", "contact_center.conversation"),
                    ("revocation_ids", "=", False),
                    "|",
                    ("derived_from_assertion_id", "=", False),
                    ("derived_from_assertion_id.revocation_ids", "=", False),
                ]
            )
        )
        return any(
            ref.isascii() and ref.isdigit() and int(ref) in live_ids
            for ref in (value or "" for value in support.mapped("authority_ref"))
        )

    def _journey_origins(self, channel, link):
        model = self.env["marketing.attribution.crm.effective.link"]
        if not model.check_access_rights("read", raise_exception=False) or not self.env[
            "marketing.attribution.effective.touchpoint"
        ].check_access_rights("read", raise_exception=False):
            return {"status": "restricted", "items": []}
        links = model.search(
            [
                ("lead_id", "=", self.id),
                ("company_id", "=", channel.contact_center_company_id.id),
            ]
        )
        partition = links._scope_partition()
        # No raw payload, identifiers or URLs; current channel authorization was checked by CRM.
        points = (
            partition["eligible"]
            .mapped("touchpoint_id")
            .filtered(
                lambda point: self.env["marketing.attribution.contact.center.link"]
                .sudo()
                .search_count(
                    [
                        (
                            "marketing_touchpoint_id.canonical_key",
                            "=",
                            point.canonical_key,
                        ),
                        (
                            "source_touchpoint_id.channel_binding_id.channel_id",
                            "=",
                            channel.id,
                        ),
                    ]
                )
            )
        )
        return {
            "status": "scope_review"
            if self._journey_scope_review_pending(partition["pending"])
            else "ready",
            "items": [
                {
                    "type": point.touchpoint_type,
                    "at": fields.Datetime.to_string(point.occurred_at),
                }
                for point in points[:3]
            ],
        }


class UiApi(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    @api.model
    def delete_conversation(self, channel_id):
        channel, _binding = self._privacy_authorized_binding(channel_id, "delete")
        links = (
            self.env["contact.center.crm.conversation.link"]
            .sudo()
            .search([("channel_id", "=", channel.id), ("state", "=", "active")])
        )
        links.mapped("lead_id")._contact_center_lock_conversation_graph(
            channel_ids=channel.ids, touch_leads=True, touch_channels=True
        )
        return super().delete_conversation(channel_id)

    def _prepare_conversation_deletion_dependencies(
        self, channel, bindings, messages, inbox_events
    ):
        links = (
            self.env["contact.center.crm.conversation.link"]
            .sudo()
            .search(
                [
                    ("channel_id", "=", channel.id),
                    ("state", "=", "active"),
                ]
            )
        )
        for lead in links.filtered("marketing_scope_pending").mapped("lead_id"):
            lead._native_utm_write({"marketing_cc_orphan_review": True})
        # Cascade deletion can orphan existing assertions even after convergence.
        # Wake every related business so its review state becomes visible promptly.
        links.mapped("lead_id")._enqueue_native_utm()
        return super()._prepare_conversation_deletion_dependencies(
            channel, bindings, messages, inbox_events
        )

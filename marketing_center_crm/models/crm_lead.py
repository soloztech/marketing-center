from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .tokens import MARKETING_CRM_EVENT_WRITE_TOKEN


class CrmLead(models.Model):
    _inherit = "crm.lead"

    marketing_event_sequence = fields.Integer(
        string="Marketing event sequence", readonly=True, copy=False, default=0
    )
    marketing_event_company_id = fields.Many2one(
        "res.company",
        string="Marketing event company",
        readonly=True,
        copy=False,
        help=(
            "Stable company scope used by the immutable marketing ledger. "
            "It is fixed on the first marketing event, including for global CRM leads."
        ),
    )
    marketing_attribution_link_ids = fields.One2many(
        "marketing.attribution.crm.link", "lead_id", readonly=True
    )
    marketing_business_event_link_ids = fields.One2many(
        "marketing.business.event.crm.link", "lead_id", readonly=True
    )
    marketing_merge_equivalence_ids = fields.One2many(
        "marketing.crm.lead.equivalence", "target_lead_id", readonly=True
    )
    marketing_touchpoint_count = fields.Integer(compute="_compute_marketing_counts")
    marketing_business_event_count = fields.Integer(compute="_compute_marketing_counts")

    def _marketing_effective_touchpoint_ids_by_lead(self):
        """Read the effective projection over assertions and revocations."""
        result = {lead_id: set() for lead_id in self.ids}
        if not self.ids:
            return result
        # This search deliberately stays in the caller's security context, so
        # the existing CRM ownership rules remain the visibility boundary.
        links = self.env["marketing.attribution.crm.effective.link"].search(
            [("lead_id", "in", self.ids)]
        )
        # This SQL view keeps a stable row id per assertion set while its
        # effective touchpoint may advance to a newer canonical revision.  ORM
        # cache invalidation is therefore required before reading the projection.
        links.invalidate_recordset(["lead_id", "touchpoint_id"])
        for link in links:
            result[link.lead_id.id].add(link.touchpoint_id.id)
        return result

    def _compute_marketing_counts(self):
        effective_by_lead = self._marketing_effective_touchpoint_ids_by_lead()
        events = self.env["marketing.business.event.crm.link"].read_group(
            [("lead_id", "in", self.ids)], ["lead_id"], ["lead_id"]
        )
        event_counts = {row["lead_id"][0]: row["lead_id_count"] for row in events}
        for lead in self:
            lead.marketing_touchpoint_count = len(effective_by_lead.get(lead.id, ()))
            lead.marketing_business_event_count = event_counts.get(lead.id, 0)

    @api.model_create_multi
    def create(self, vals_list):
        leads = super().create(vals_list)
        if (
            self.env.context.get("marketing_crm_event_write_token")
            is MARKETING_CRM_EVENT_WRITE_TOKEN
        ):
            return leads
        service = self.env["marketing.crm.service"]
        for lead in leads:
            sequence = lead._marketing_next_event_sequence()
            service._ingest_lead_event(
                lead,
                "lead_created",
                "created",
                occurred_at=lead.create_date or fields.Datetime.now(),
                extensions={
                    "crm.lead_type": lead.type,
                    "crm.transition_sequence": sequence,
                },
            )
        return leads

    def write(self, values):
        internal_write = (
            self.env.context.get("marketing_crm_event_write_token")
            is MARKETING_CRM_EVENT_WRITE_TOKEN
        )
        internal_fields = {"marketing_event_sequence", "marketing_event_company_id"}
        if internal_fields & set(values) and not internal_write:
            raise AccessError(_("The marketing event scope is managed internally."))
        if internal_write:
            return super().write(values)
        if "company_id" in values:
            requested_company_id = values.get("company_id") or False
            anchored = self.filtered("marketing_event_company_id")
            invalid = anchored.filtered(
                lambda lead: requested_company_id
                and requested_company_id != lead.marketing_event_company_id.id
            )
            if invalid:
                raise ValidationError(
                    _(
                        "A CRM lead with immutable marketing evidence cannot be moved "
                        "to another company. Archive or duplicate it in the target "
                        "company instead."
                    )
                )
        tracked = {"stage_id", "active", "lost_reason_id"} & set(values)
        if not tracked:
            return super().write(values)
        self._marketing_lock_event_state()
        before = {
            lead.id: {
                "stage": lead.stage_id,
                "active": lead.active,
                "lost_reason": lead.lost_reason_id,
            }
            for lead in self
        }
        watermark = self._marketing_tracking_watermark()
        result = super().write(values)
        tracking_by_lead = self._marketing_new_tracking_by_lead(watermark)
        service = self.env["marketing.crm.service"]
        now = fields.Datetime.now()
        for lead in self:
            previous = before[lead.id]
            stage_changed = previous["stage"] != lead.stage_id
            became_lost = previous["active"] and not lead.active
            if not stage_changed and not became_lost:
                continue
            sequence = lead._marketing_next_event_sequence()
            by_field = tracking_by_lead.get(lead.id, {})
            if stage_changed:
                tracking = by_field.get("stage_id")
                occurrence_ref = (
                    "tracking:%s" % tracking.id
                    if tracking
                    else "sequence:%s" % sequence
                )
                service._emit_stage_events(
                    lead,
                    previous["stage"],
                    lead.stage_id,
                    occurrence_ref,
                    occurred_at=tracking.mail_message_id.date if tracking else now,
                    sequence=sequence,
                )
            if became_lost:
                tracking = by_field.get("active") or by_field.get("lost_reason_id")
                occurrence_ref = (
                    "tracking:%s" % tracking.id
                    if tracking
                    else "sequence:%s" % sequence
                )
                service._emit_lost_event(
                    lead,
                    occurrence_ref,
                    occurred_at=tracking.mail_message_id.date if tracking else now,
                    sequence=sequence,
                    lost_reason=(
                        lead.lost_reason_id
                        or self.env["crm.lost.reason"].browse(
                            self.env.context.get("marketing_crm_lost_reason_id")
                        )
                        or previous["lost_reason"]
                    ),
                )
        return result

    def _merge_opportunity(
        self, user_id=False, team_id=False, auto_unlink=True, max_length=5
    ):
        """Preserve and re-project marketing evidence across native CRM merges."""

        service = self.env["marketing.crm.service"]
        merge_payloads = (
            service._prepare_lead_merge(self) if auto_unlink and len(self) > 1 else []
        )
        merged = super()._merge_opportunity(
            user_id=user_id,
            team_id=team_id,
            auto_unlink=auto_unlink,
            max_length=max_length,
        )
        if merge_payloads:
            service._record_lead_merge(merged, merge_payloads)
        return merged

    def _marketing_lock_event_state(self):
        if not self.ids:
            return True
        self.env.cr.execute(
            "SELECT id FROM crm_lead WHERE id IN %s ORDER BY id FOR UPDATE",
            [tuple(self.ids)],
        )
        self.invalidate_recordset(
            ["stage_id", "active", "lost_reason_id", "marketing_event_sequence"]
        )
        return True

    def action_set_lost(self, **additional_values):
        # Odoo archives first and writes lost_reason_id afterwards. Carry the
        # wizard's explicit reason into the archive transaction so the immutable
        # lost event records the reason at the moment the loss occurs.
        reason_id = additional_values.get("lost_reason_id")
        leads = self.with_context(marketing_crm_lost_reason_id=reason_id)
        return super(CrmLead, leads).action_set_lost(**additional_values)

    def _marketing_next_event_sequence(self):
        self.ensure_one()
        self.env.cr.execute(
            "SELECT marketing_event_sequence FROM crm_lead WHERE id = %s FOR UPDATE",
            [self.id],
        )
        row = self.env.cr.fetchone()
        sequence = (row[0] if row else 0) + 1
        super(
            CrmLead,
            self.with_context(
                marketing_crm_event_write_token=MARKETING_CRM_EVENT_WRITE_TOKEN
            ),
        ).write({"marketing_event_sequence": sequence})
        return sequence

    def _marketing_tracking_watermark(self):
        tracking = (
            self.env["mail.tracking.value"].sudo().search([], order="id desc", limit=1)
        )
        return tracking.id or 0

    def _marketing_new_tracking_by_lead(self, watermark):
        values = (
            self.env["mail.tracking.value"]
            .sudo()
            .search(
                [
                    ("id", ">", watermark),
                    ("mail_message_id.model", "=", "crm.lead"),
                    ("mail_message_id.res_id", "in", self.ids),
                    ("field.name", "in", ["stage_id", "active", "lost_reason_id"]),
                ],
                order="id asc",
            )
        )
        result = {}
        for value in values:
            result.setdefault(value.mail_message_id.res_id, {})[
                value.field.name
            ] = value
        return result

    def action_view_marketing_touchpoints(self):
        self.ensure_one()
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_admin"
        ):
            raise AccessError(
                _("Only Marketing administrators can inspect touchpoints.")
            )
        effective_ids = self._marketing_effective_touchpoint_ids_by_lead().get(
            self.id, set()
        )
        touchpoints = self.env["marketing.attribution.effective.touchpoint"].browse(
            sorted(effective_ids)
        )
        action = self.env["ir.actions.actions"]._for_xml_id(
            "marketing_center_base.action_marketing_attribution_effective_touchpoints"
        )
        action.update(
            {"domain": [("id", "in", touchpoints.ids)], "context": {"create": False}}
        )
        if len(touchpoints) == 1:
            action.update(
                {
                    "res_id": touchpoints.id,
                    "view_mode": "form",
                    "views": [(False, "form")],
                }
            )
        return action

    def action_view_marketing_business_events(self):
        self.ensure_one()
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_analyst"
        ):
            raise AccessError(
                _("Marketing Analyst access is required to inspect events.")
            )
        events = self.marketing_business_event_link_ids.mapped("event_id")
        action = self.env["ir.actions.actions"]._for_xml_id(
            "marketing_center_base.action_marketing_business_events"
        )
        action.update(
            {"domain": [("id", "in", events.ids)], "context": {"create": False}}
        )
        if len(events) == 1:
            action.update(
                {"res_id": events.id, "view_mode": "form", "views": [(False, "form")]}
            )
        return action

    def action_backfill_marketing_events(self):
        self.ensure_one()
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_manager"
        ):
            raise AccessError(_("Only Marketing managers can reconcile CRM history."))
        self.check_access_rights("read")
        self.check_access_rule("read")
        service = self.env["marketing.crm.service"]
        cursor = 0
        processed = 0
        for _page in range(100):
            result = service._backfill_lead_events(
                self, after_tracking_id=cursor, limit=500
            )
            processed += result["processed"]
            if not result["has_more"]:
                break
            next_cursor = result["last_tracking_id"]
            if next_cursor <= cursor:
                raise ValidationError(_("CRM history reconciliation did not advance."))
            cursor = next_cursor
        else:
            raise ValidationError(
                _(
                    "CRM history exceeds the synchronous reconciliation safety limit. "
                    "Run the paginated service from a queue job."
                )
            )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Marketing events"),
                "message": _("CRM history reconciled: %s tracking rows.") % processed,
                "type": "success",
                "sticky": False,
            },
        }

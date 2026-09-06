from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .service import CONFIRMED_STATES
from .tokens import MARKETING_SALE_INTERNAL_WRITE_TOKEN, MARKETING_SALE_TRANSITION_GUARD


class SaleOrder(models.Model):
    _inherit = "sale.order"

    marketing_sale_event_sequence = fields.Integer(
        string="Marketing event sequence", readonly=True, copy=False, default=0
    )
    marketing_sale_company_id = fields.Many2one(
        "res.company",
        string="Marketing event company",
        index=True,
        readonly=True,
        copy=False,
        help=(
            "Stable company scope used by the immutable marketing ledger for this "
            "sales order."
        ),
    )
    marketing_crm_link_ids = fields.One2many(
        "marketing.sale.order.crm.link", "order_id", readonly=True
    )
    marketing_event_link_ids = fields.One2many(
        "marketing.business.event.sale.link", "order_id", readonly=True
    )
    marketing_crm_lead_count = fields.Integer(compute="_compute_marketing_counts")
    marketing_business_event_count = fields.Integer(compute="_compute_marketing_counts")

    def _compute_marketing_counts(self):
        crm_grouped = self.env["marketing.sale.order.crm.link"].read_group(
            [("order_id", "in", self.ids), ("lead_id", "!=", False)],
            ["order_id"],
            ["order_id"],
        )
        event_grouped = self.env["marketing.business.event.sale.link"].read_group(
            [("order_id", "in", self.ids)], ["order_id"], ["order_id"]
        )
        crm_counts = {row["order_id"][0]: row["order_id_count"] for row in crm_grouped}
        event_counts = {
            row["order_id"][0]: row["order_id_count"] for row in event_grouped
        }
        for order in self:
            order.marketing_crm_lead_count = crm_counts.get(order.id, 0)
            order.marketing_business_event_count = event_counts.get(order.id, 0)

    @api.model_create_multi
    def create(self, vals_list):
        internal = (
            self.env.context.get("marketing_sale_internal_write_token")
            is MARKETING_SALE_INTERNAL_WRITE_TOKEN
        )
        protected = {"marketing_sale_event_sequence", "marketing_sale_company_id"}
        if not internal and (
            any(protected & set(values) for values in vals_list)
            or any(self.env.context.get("default_%s" % name) for name in protected)
        ):
            raise AccessError(_("The sales marketing scope is managed internally."))
        orders = super().create(vals_list)
        service = self.env["marketing.sale.service"]
        for order in orders.filtered("opportunity_id"):
            service._link_order_lead(order, order.opportunity_id, "sale.create")
        return orders

    def write(self, values):
        internal = (
            self.env.context.get("marketing_sale_internal_write_token")
            is MARKETING_SALE_INTERNAL_WRITE_TOKEN
        )
        guarded = (
            self.env.context.get("marketing_sale_transition_guard")
            is MARKETING_SALE_TRANSITION_GUARD
        )
        protected = {"marketing_sale_event_sequence", "marketing_sale_company_id"}
        if protected & set(values) and not internal:
            raise AccessError(_("The sales marketing scope is managed internally."))
        if internal or guarded:
            return super().write(values)
        if "company_id" in values:
            requested_company_id = values.get("company_id") or False
            invalid = self.filtered(
                lambda order: order.marketing_sale_company_id
                and requested_company_id != order.marketing_sale_company_id.id
            )
            if invalid:
                raise ValidationError(
                    _(
                        "A sales order with immutable marketing scope cannot move "
                        "to another company. Duplicate it in the target company."
                    )
                )
        tracks_proposal = values.get("state") == "sent"
        tracks_lead = "opportunity_id" in values
        if not tracks_proposal and not tracks_lead:
            return super().write(values)
        if tracks_proposal:
            self._marketing_sale_lock_event_state()
            previous_states = {order.id: order.state for order in self}
            watermark = self._marketing_sale_tracking_watermark()
        else:
            previous_states = {}
            watermark = 0
        result = super().write(values)
        service = self.env["marketing.sale.service"]
        if tracks_lead:
            for order in self.filtered("opportunity_id"):
                service._link_order_lead(
                    order, order.opportunity_id, "sale.opportunity_id.write"
                )
        if tracks_proposal:
            tracking_by_order = self._marketing_sale_new_state_tracking(watermark)
            for order in self:
                if previous_states[order.id] != "draft" or order.state != "sent":
                    continue
                if service._first_proposal_event(order):
                    continue
                sequence = order._marketing_sale_next_event_sequence()
                tracking = tracking_by_order.get(order.id)
                occurrence_ref = "sequence:%s" % sequence
                service._emit_order_event(
                    order,
                    "proposal_sent",
                    occurrence_ref,
                    occurred_at=(
                        tracking.mail_message_id.date
                        if tracking
                        else fields.Datetime.now()
                    ),
                    old_state="draft",
                    new_state="sent",
                    sequence=sequence,
                    tracking_watermark=watermark,
                    evidence_ref=(
                        "tracking:%s" % tracking.id if tracking else occurrence_ref
                    ),
                )
        return result

    def action_confirm(self):
        if (
            self.env.context.get("marketing_sale_transition_guard")
            is MARKETING_SALE_TRANSITION_GUARD
        ):
            return super().action_confirm()
        self._marketing_sale_lock_event_state()
        previous_states = {order.id: order.state for order in self}
        watermark = self._marketing_sale_tracking_watermark()
        result = super().action_confirm()
        tracking_by_order = self._marketing_sale_new_state_tracking(watermark)
        service = self.env["marketing.sale.service"]
        for order in self:
            if (
                previous_states[order.id] in CONFIRMED_STATES
                or order.state not in CONFIRMED_STATES
            ):
                continue
            sequence = order._marketing_sale_next_event_sequence()
            tracking = tracking_by_order.get(order.id)
            occurrence_ref = "sequence:%s" % sequence
            service._emit_order_event(
                order,
                "order_confirmed",
                occurrence_ref,
                occurred_at=(
                    tracking.mail_message_id.date
                    if tracking
                    else order.date_order or fields.Datetime.now()
                ),
                old_state=previous_states[order.id],
                new_state=order.state,
                sequence=sequence,
                tracking_watermark=watermark,
                evidence_ref=(
                    "tracking:%s" % tracking.id if tracking else occurrence_ref
                ),
            )
        return result

    def _action_cancel(self):
        if (
            self.env.context.get("marketing_sale_transition_guard")
            is MARKETING_SALE_TRANSITION_GUARD
        ):
            return super()._action_cancel()
        self._marketing_sale_lock_event_state()
        previous_states = {order.id: order.state for order in self}
        watermark = self._marketing_sale_tracking_watermark()
        result = super()._action_cancel()
        tracking_by_order = self._marketing_sale_new_state_tracking(watermark)
        service = self.env["marketing.sale.service"]
        for order in self:
            if (
                previous_states[order.id] not in CONFIRMED_STATES
                or order.state != "cancel"
            ):
                continue
            confirmation = service._open_confirmation(order)
            if not confirmation:
                confirmation = service._ensure_confirmation(order)
            sequence = order._marketing_sale_next_event_sequence()
            tracking = tracking_by_order.get(order.id)
            occurrence_ref = "sequence:%s" % sequence
            service._emit_order_event(
                order,
                "order_cancelled",
                occurrence_ref,
                occurred_at=(
                    tracking.mail_message_id.date if tracking else fields.Datetime.now()
                ),
                old_state=previous_states[order.id],
                new_state="cancel",
                sequence=sequence,
                tracking_watermark=watermark,
                reversed_event=confirmation,
                evidence_ref=(
                    "tracking:%s" % tracking.id if tracking else occurrence_ref
                ),
            )
        return result

    def _marketing_sale_lock_event_state(self):
        if not self.ids:
            return True
        self.env.cr.execute(
            "SELECT id FROM sale_order WHERE id IN %s ORDER BY id FOR UPDATE",
            [tuple(self.ids)],
        )
        self.invalidate_recordset(
            ["state", "marketing_sale_event_sequence", "marketing_sale_company_id"]
        )
        return True

    def _marketing_sale_next_event_sequence(self):
        self.ensure_one()
        self.env.cr.execute(
            "SELECT marketing_sale_event_sequence FROM sale_order WHERE id = %s FOR UPDATE",
            [self.id],
        )
        row = self.env.cr.fetchone()
        sequence = (row[0] if row else 0) + 1
        super(
            SaleOrder,
            self.with_context(
                marketing_sale_internal_write_token=MARKETING_SALE_INTERNAL_WRITE_TOKEN
            ),
        ).write({"marketing_sale_event_sequence": sequence})
        return sequence

    def _marketing_sale_tracking_watermark(self):
        tracking = (
            self.env["mail.tracking.value"].sudo().search([], order="id desc", limit=1)
        )
        return tracking.id or 0

    def _marketing_sale_new_state_tracking(self, watermark):
        values = (
            self.env["mail.tracking.value"]
            .sudo()
            .search(
                [
                    ("id", ">", watermark),
                    ("mail_message_id.model", "=", "sale.order"),
                    ("mail_message_id.res_id", "in", self.ids),
                    ("field.name", "=", "state"),
                ],
                order="id asc",
            )
        )
        result = {}
        for value in values:
            # In auto-done mode confirmation can track draft/sent -> sale and
            # then sale -> done in the same call. The first row is the evidence
            # for entering the confirmed state.
            result.setdefault(value.mail_message_id.res_id, value)
        return result

    def action_view_marketing_crm_leads(self):
        self.ensure_one()
        leads = self.marketing_crm_link_ids.mapped("lead_id").exists()
        return self.env["marketing.crm.service"]._lead_action(leads)

    def action_view_marketing_business_events(self):
        self.ensure_one()
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_analyst"
        ):
            raise AccessError(
                _("Marketing Analyst access is required to inspect events.")
            )
        events = self.marketing_event_link_ids.mapped("event_id")
        events.check_access_rights("read")
        events.check_access_rule("read")
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
            raise AccessError(_("Only Marketing managers can reconcile sales history."))
        self.check_access_rights("read")
        self.check_access_rule("read")
        service = self.env["marketing.sale.service"]
        cursor = 0
        processed = 0
        for _page in range(100):
            result = service._backfill_order_events(
                self, after_tracking_id=cursor, limit=500
            )
            processed += result["processed"]
            if not result["has_more"]:
                break
            next_cursor = result["last_tracking_id"]
            if next_cursor <= cursor:
                raise ValidationError(
                    _("Sales history reconciliation did not advance.")
                )
            cursor = next_cursor
        else:
            raise ValidationError(
                _(
                    "Sales history exceeds the synchronous reconciliation safety "
                    "limit. Run the paginated service from a queue job."
                )
            )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Marketing events"),
                "message": _("Sales history reconciled: %s tracking rows.", processed),
                "type": "success",
                "sticky": False,
            },
        }

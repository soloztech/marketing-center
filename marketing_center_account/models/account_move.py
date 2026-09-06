from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .service import OUTGOING_MOVE_TYPES
from .tokens import (
    MARKETING_ACCOUNT_INTERNAL_WRITE_TOKEN,
    MARKETING_ACCOUNT_TRANSITION_GUARD,
)


class AccountMove(models.Model):
    _inherit = "account.move"

    marketing_account_event_sequence = fields.Integer(
        string="Marketing event sequence", readonly=True, copy=False, default=0
    )
    marketing_account_company_id = fields.Many2one(
        "res.company",
        string="Marketing event company",
        index=True,
        readonly=True,
        copy=False,
        help=(
            "Stable company scope used by the immutable marketing ledger for this "
            "journal entry."
        ),
    )
    marketing_account_event_link_ids = fields.One2many(
        "marketing.business.event.account.move.link",
        "move_id",
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_analyst",
    )
    marketing_business_event_count = fields.Integer(
        compute="_compute_marketing_business_event_count",
        groups="marketing_center_base.group_marketing_center_analyst",
    )

    def _compute_marketing_business_event_count(self):
        grouped = self.env["marketing.business.event.account.move.link"].read_group(
            [("move_id", "in", self.ids)], ["move_id"], ["move_id"]
        )
        counts = {row["move_id"][0]: row["move_id_count"] for row in grouped}
        for move in self:
            move.marketing_business_event_count = counts.get(move.id, 0)

    @api.model_create_multi
    def create(self, vals_list):
        internal = (
            self.env.context.get("marketing_account_internal_write_token")
            is MARKETING_ACCOUNT_INTERNAL_WRITE_TOKEN
        )
        protected_values = any(
            values.get("marketing_account_event_sequence") not in (None, False, 0)
            or bool(values.get("marketing_account_company_id"))
            for values in vals_list
        )
        protected_defaults = any(
            self.env.context.get("default_%s" % name)
            for name in (
                "marketing_account_event_sequence",
                "marketing_account_company_id",
            )
        )
        if not internal and (protected_values or protected_defaults):
            raise AccessError(
                _("The accounting marketing scope is managed internally.")
            )
        return super().create(vals_list)

    def write(self, values):
        internal = (
            self.env.context.get("marketing_account_internal_write_token")
            is MARKETING_ACCOUNT_INTERNAL_WRITE_TOKEN
        )
        protected = {
            "marketing_account_event_sequence",
            "marketing_account_company_id",
        }
        if protected & set(values) and not internal:
            raise AccessError(
                _("The accounting marketing scope is managed internally.")
            )
        if not internal and ({"company_id", "journal_id"} & set(values)):
            requested_company_ids = set()
            if "company_id" in values:
                requested_company_ids.add(values.get("company_id") or False)
            if "journal_id" in values:
                journal = self.env["account.journal"].browse(
                    values.get("journal_id") or False
                )
                requested_company_ids.add(journal.company_id.id or False)
            invalid = self.filtered(
                lambda move: move.marketing_account_company_id
                and requested_company_ids != {move.marketing_account_company_id.id}
            )
            if invalid:
                raise ValidationError(
                    _(
                        "A journal entry with immutable marketing scope cannot use "
                        "a company or journal from another company."
                    )
                )
        return super().write(values)

    def _post(self, soft=True):
        if (
            self.env.context.get("marketing_account_transition_guard")
            is MARKETING_ACCOUNT_TRANSITION_GUARD
        ):
            return super()._post(soft=soft)
        candidates = self.filtered(
            lambda move: move.move_type in OUTGOING_MOVE_TYPES
            and move.state != "posted"
        )
        candidates._marketing_account_lock_event_state()
        previous_states = {move.id: move.state for move in candidates}
        result = super()._post(soft=soft)
        service = self.env["marketing.account.service"]
        for move in candidates:
            if previous_states[move.id] == "posted" or move.state != "posted":
                continue
            sequence = move._marketing_account_next_event_sequence()
            occurrence_ref = "sequence:%s" % sequence
            service._emit_move_event(
                move,
                occurrence_ref,
                occurred_at=fields.Datetime.now(),
                sequence=sequence,
                evidence_ref=occurrence_ref,
            )
        return result

    def _marketing_account_lock_event_state(self):
        if not self.ids:
            return True
        self.env.cr.execute(
            "SELECT id FROM account_move WHERE id IN %s ORDER BY id FOR UPDATE",
            [tuple(self.ids)],
        )
        self.invalidate_recordset(
            [
                "state",
                "marketing_account_event_sequence",
                "marketing_account_company_id",
            ]
        )
        return True

    def _marketing_account_next_event_sequence(self):
        self.ensure_one()
        self.env.cr.execute(
            "SELECT marketing_account_event_sequence FROM account_move "
            "WHERE id = %s FOR UPDATE",
            [self.id],
        )
        row = self.env.cr.fetchone()
        sequence = (row[0] if row else 0) + 1
        super(
            AccountMove,
            self.with_context(
                marketing_account_internal_write_token=(
                    MARKETING_ACCOUNT_INTERNAL_WRITE_TOKEN
                )
            ),
        ).write({"marketing_account_event_sequence": sequence})
        return sequence

    def action_view_marketing_business_events(self):
        self.ensure_one()
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_analyst"
        ):
            raise AccessError(
                _("Marketing Analyst access is required to inspect events.")
            )
        events = self.marketing_account_event_link_ids.mapped("event_id")
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
            raise AccessError(
                _("Only Marketing managers can reconcile accounting history.")
            )
        self.check_access_rights("read")
        self.check_access_rule("read")
        event = self.env["marketing.account.service"]._ensure_move_event(self)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Marketing events"),
                "message": (
                    _("Accounting event reconciled.")
                    if event
                    else _("This journal entry is not an eligible customer document.")
                ),
                "type": "success" if event else "warning",
                "sticky": False,
            },
        }


class AccountPayment(models.Model):
    _inherit = "account.payment"

    marketing_account_company_id = fields.Many2one(
        "res.company",
        string="Marketing event company",
        index=True,
        readonly=True,
        copy=False,
        help=(
            "Stable company scope used by the immutable marketing ledger for this "
            "payment."
        ),
    )
    marketing_account_event_link_ids = fields.One2many(
        "marketing.business.event.account.payment.link",
        "payment_id",
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_analyst",
    )
    marketing_business_event_count = fields.Integer(
        compute="_compute_marketing_business_event_count",
        groups="marketing_center_base.group_marketing_center_analyst",
    )

    @api.model_create_multi
    def create(self, vals_list):
        internal = (
            self.env.context.get("marketing_account_internal_write_token")
            is MARKETING_ACCOUNT_INTERNAL_WRITE_TOKEN
        )
        if not internal and (
            any("marketing_account_company_id" in values for values in vals_list)
            or self.env.context.get("default_marketing_account_company_id")
        ):
            raise AccessError(_("The payment marketing scope is managed internally."))
        return super().create(vals_list)

    def write(self, values):
        internal = (
            self.env.context.get("marketing_account_internal_write_token")
            is MARKETING_ACCOUNT_INTERNAL_WRITE_TOKEN
        )
        if "marketing_account_company_id" in values and not internal:
            raise AccessError(_("The payment marketing scope is managed internally."))
        if not internal:
            requested_company = self.env["res.company"]
            if values.get("journal_id"):
                requested_company = (
                    self.env["account.journal"].browse(values["journal_id"]).company_id
                )
            elif values.get("company_id"):
                requested_company = self.env["res.company"].browse(values["company_id"])
            if requested_company:
                invalid = self.filtered(
                    lambda payment: payment.marketing_account_company_id
                    and payment.marketing_account_company_id != requested_company
                )
                if invalid:
                    raise ValidationError(
                        _(
                            "A payment with immutable marketing scope cannot move "
                            "to another company."
                        )
                    )
        return super().write(values)

    def _compute_marketing_business_event_count(self):
        grouped = self.env["marketing.business.event.account.payment.link"].read_group(
            [("payment_id", "in", self.ids)], ["payment_id"], ["payment_id"]
        )
        counts = {row["payment_id"][0]: row["payment_id_count"] for row in grouped}
        for payment in self:
            payment.marketing_business_event_count = counts.get(payment.id, 0)

    def action_view_marketing_business_events(self):
        self.ensure_one()
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_analyst"
        ):
            raise AccessError(
                _("Marketing Analyst access is required to inspect events.")
            )
        events = self.marketing_account_event_link_ids.mapped("event_id")
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
            raise AccessError(
                _("Only Marketing managers can reconcile accounting history.")
            )
        self.check_access_rights("read")
        self.check_access_rule("read")
        cursor = 0
        processed = 0
        emitted = 0
        service = self.env["marketing.account.service"]
        for _page in range(100):
            result = service._backfill_payment(self, after_id=cursor, limit=500)
            processed += result["processed"]
            emitted += result["emitted"]
            if result["processed"] < 500:
                break
            next_cursor = result["last_id"]
            if next_cursor <= cursor:
                raise ValidationError(_("Accounting reconciliation did not advance."))
            cursor = next_cursor
        else:
            raise ValidationError(
                _(
                    "Payment history exceeds the synchronous reconciliation safety "
                    "limit. Run the paginated service from a queue job."
                )
            )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Marketing events"),
                "message": _(
                    "Payment history reconciled: %(processed)s rows, %(emitted)s "
                    "eligible allocations.",
                    processed=processed,
                    emitted=emitted,
                ),
                "type": "success",
                "sticky": False,
            },
        }

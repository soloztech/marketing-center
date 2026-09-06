from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from .tokens import (
    MARKETING_ACCOUNT_INTERNAL_WRITE_TOKEN,
    MARKETING_ACCOUNT_TRANSITION_GUARD,
)


class AccountPartialReconcile(models.Model):
    _inherit = "account.partial.reconcile"

    marketing_account_event_claimed = fields.Boolean(
        string="Marketing allocation event claimed",
        default=False,
        readonly=True,
        copy=False,
        help=(
            "Internal transaction claim used to serialize the first projection of "
            "this payment allocation."
        ),
    )

    @api.model_create_multi
    def create(self, vals_list):
        internal = (
            self.env.context.get("marketing_account_internal_write_token")
            is MARKETING_ACCOUNT_INTERNAL_WRITE_TOKEN
        )
        if not internal and (
            any("marketing_account_event_claimed" in values for values in vals_list)
            or self.env.context.get("default_marketing_account_event_claimed")
        ):
            raise AccessError(
                _("The payment-allocation marketing claim is managed internally.")
            )
        partials = super().create(vals_list)
        if (
            self.env.context.get("marketing_account_transition_guard")
            is MARKETING_ACCOUNT_TRANSITION_GUARD
        ):
            return partials
        service = self.env["marketing.account.service"]
        for partial in partials.sorted("id"):
            service._ensure_allocation_event(partial)
        return partials

    def write(self, values):
        internal = (
            self.env.context.get("marketing_account_internal_write_token")
            is MARKETING_ACCOUNT_INTERNAL_WRITE_TOKEN
        )
        if "marketing_account_event_claimed" in values and not internal:
            raise AccessError(
                _("The payment-allocation marketing claim is managed internally.")
            )
        return super().write(values)

    def unlink(self):
        if not self:
            return True
        if (
            self.env.context.get("marketing_account_transition_guard")
            is MARKETING_ACCOUNT_TRANSITION_GUARD
        ):
            return super().unlink()
        service = self.env["marketing.account.service"]
        reversals = []
        for partial in self.sorted("id"):
            event, facts = service._ensure_allocation_event(partial)
            if event and facts:
                reversals.append((event, facts))
        result = super().unlink()
        for event, facts in reversals:
            service._emit_allocation_reversal(event, facts)
        return result

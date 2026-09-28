from odoo import api, fields, models

from odoo.addons.marketing_center_base.services import capture_policy


class ResCompany(models.Model):
    _inherit = "res.company"

    marketing_crm_events_enabled = fields.Boolean(
        string="Capture marketing CRM events",
        default=True,
        help=(
            "Record lead creation and meaningful CRM lifecycle transitions. "
            "Independent of the optional sales, accounting and conversation ledger. "
            "Enabling this does not rebuild past events or send conversions to ads."
        ),
    )

    marketing_crm_events_changed_at = fields.Datetime(
        string="CRM event capture changed at",
        readonly=True,
        copy=False,
        help=(
            "When the CRM event capture policy last changed, or when this tracking "
            "was introduced for a company with capture already enabled. Empty means "
            "the start of the current state is unknown."
        ),
    )

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [
            capture_policy.neutralize_create_stamp(
                self, vals, "marketing_crm_events_changed_at"
            )
            for vals in vals_list
        ]
        companies = super().create(vals_list)
        capture_policy.stamp_created(
            companies, "marketing_crm_events_enabled", "marketing_crm_events_changed_at"
        )
        return companies

    def write(self, values):
        return capture_policy.write_with_stamp(
            self,
            values,
            super().write,
            "marketing_crm_events_enabled",
            "marketing_crm_events_changed_at",
        )

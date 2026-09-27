from odoo import _, api, fields, models

from ..services import capture_policy


class ResCompany(models.Model):
    _inherit = "res.company"

    marketing_business_events_enabled = fields.Boolean(
        string="Capture marketing business events",
        default=False,
        help=(
            "Enable the optional business-event ledger and its projections. "
            "Disabled by default while acquisition and CRM are consolidated. "
            "Disabling capture preserves existing history."
        ),
    )

    marketing_business_events_changed_at = fields.Datetime(
        string="Business-event capture changed at",
        readonly=True,
        copy=False,
        help=(
            "When the business-event capture policy last changed, or when this "
            "tracking was introduced for a company with capture already enabled. "
            "Empty means the start of the current state is unknown."
        ),
    )

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [
            capture_policy.neutralize_create_stamp(
                self, vals, "marketing_business_events_changed_at"
            )
            for vals in vals_list
        ]
        companies = super().create(vals_list)
        capture_policy.stamp_created(
            companies,
            "marketing_business_events_enabled",
            "marketing_business_events_changed_at",
        )
        return companies

    def write(self, values):
        return capture_policy.write_with_stamp(
            self,
            values,
            super().write,
            "marketing_business_events_enabled",
            "marketing_business_events_changed_at",
        )

    @api.model
    def _marketing_initialize_capture_stamp(self, flag_field, stamp_field):
        """Upgrade helper for this module and its optional policy extensions."""

        return capture_policy.initialize_stamp(self.env, flag_field, stamp_field)

    def _marketing_business_events_disabled_notification(self):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Marketing events paused"),
                "message": _(
                    "Business-event capture is disabled for this company. "
                    "Existing history is preserved; no history was rebuilt."
                ),
                "type": "info",
                "sticky": False,
            },
        }

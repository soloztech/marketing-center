from odoo import _, fields, models


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

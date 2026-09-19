from odoo import fields, models


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

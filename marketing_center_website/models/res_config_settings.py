from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    marketing_cookie_notice_text = fields.Text(
        related="website_id.marketing_cookie_notice_text", readonly=False,
    )
    marketing_cookie_proceed_label = fields.Char(
        related="website_id.marketing_cookie_proceed_label", readonly=False,
    )
    marketing_cookie_policy_url = fields.Char(
        related="website_id.marketing_cookie_policy_url", readonly=False,
    )

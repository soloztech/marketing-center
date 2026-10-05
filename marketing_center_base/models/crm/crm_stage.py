from odoo import fields, models


class CrmStage(models.Model):
    _inherit = "crm.stage"

    marketing_semantic = fields.Selection(
        [("qualified", "Qualified")],
        string="Marketing semantic",
        help=(
            "Emit a qualified marketing event when a lead enters this stage. "
            "Leave empty when the stage has no explicit marketing meaning."
        ),
    )

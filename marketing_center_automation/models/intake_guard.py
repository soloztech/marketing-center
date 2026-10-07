"""Intake-created CRM records are permanently excluded from automation."""
from odoo import models


class BaseAutomation(models.Model):
    _inherit = "base.automation"

    _contact_center_intake_guard = 1

    def _process(self, records, domain_post=None):
        if records._name == "crm.lead":
            records = records.filtered(
                lambda lead: not lead.sudo().contact_center_intake_created
            )
        if not records:
            return None
        return super()._process(records, domain_post=domain_post)


class IrActionsServer(models.Model):
    _inherit = "ir.actions.server"

    def run(self):
        # Native on_change rules bypass base.automation._process and pass this
        # pseudo-record in context. Keep normal manual/server actions unchanged.
        onchange = self.env.context.get("onchange_self")
        if getattr(onchange, "_name", None) == "crm.lead":
            origin = onchange._origin
            if origin and origin.sudo().contact_center_intake_created:
                return False
        return super().run()

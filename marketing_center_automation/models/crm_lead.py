from odoo import fields, models


class CrmLead(models.Model):
    _inherit = "crm.lead"

    cc_automation_paused = fields.Boolean(
        string="Pausar mensagens automáticas", copy=False
    )

    def _cc_queue_automation_entry(self):
        # base_automation calls this inside lead creation. Only enqueue; queue_job
        # makes enrollment visible to workers after the business transaction commits.
        for lead in self.filtered(lambda record: record.type == "lead"):
            configurations = (
                self.env["automation.configuration"]
                .sudo()
                .search(
                    [
                        ("model", "=", "crm.lead"),
                        ("state", "=", "periodic"),
                        ("cc_lead_entry", "=", True),
                        ("cc_send_enabled", "=", True),
                        ("company_id", "=", lead.company_id.id),
                        ("cc_entry_after", "<=", lead.create_date),
                        ("cc_entry_lead_id", "<", lead.id),
                    ]
                )
            )
            for configuration in configurations:
                configuration.with_delay(
                    identity_key="marketing_automation:entry:%s:%s"
                    % (configuration.id, lead.id),
                    description="Marketing: enroll new lead %s" % lead.id,
                )._job_cc_enroll_lead(lead.id)

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.osv import expression
from odoo.tools.safe_eval import safe_eval


def require_cc_administrator(env):
    if not env.su and not env.user.has_group("base.group_system"):
        raise AccessError(
            _("Only a system administrator can configure communication workflows.")
        )


class AutomationConfiguration(models.Model):
    _inherit = "automation.configuration"

    cc_lead_entry = fields.Boolean(string="Entrada automática de leads", copy=False)
    cc_send_enabled = fields.Boolean(
        string="Permitir mensagens automáticas", copy=False
    )
    cc_entry_after = fields.Datetime(
        string="Receber leads criados após", readonly=True, copy=False
    )
    cc_entry_lead_id = fields.Integer(readonly=True, copy=False)
    cc_execution_user_id = fields.Many2one(
        "res.users", string="Usuário de execução", domain=[("share", "=", False)]
    )

    def _cc_is_workflow(self):
        self.ensure_one()
        return self.cc_lead_entry or any(
            step.step_type == "contact_center" for step in self.automation_step_ids
        )

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [dict(values) for values in vals_list]
        for values in vals_list:
            if values.get("cc_lead_entry") or any(
                key.startswith("cc_") for key in values
            ):
                require_cc_administrator(self.env)
            if values.get(
                "cc_send_enabled", self.env.context.get("default_cc_send_enabled")
            ):
                values["cc_entry_after"] = fields.Datetime.now()
                values["cc_entry_lead_id"] = (
                    self.env["crm.lead"].sudo().search([], order="id desc", limit=1).id
                )
        result = super().create(vals_list)
        if any(
            configuration._cc_is_workflow() or configuration.cc_send_enabled
            for configuration in result
        ):
            require_cc_administrator(self.env)
        return result

    def write(self, values):
        if any(key.startswith("cc_") for key in values) or any(
            configuration._cc_is_workflow() for configuration in self
        ):
            require_cc_administrator(self.env)
        if values.get("cc_send_enabled"):
            for configuration in self:
                updated = dict(values)
                if not configuration.cc_send_enabled:
                    updated["cc_entry_after"] = fields.Datetime.now()
                    updated["cc_entry_lead_id"] = (
                        self.env["crm.lead"]
                        .sudo()
                        .search([], order="id desc", limit=1)
                        .id
                    )
                super(AutomationConfiguration, configuration).write(updated)
            return True
        return super().write(values)

    def unlink(self):
        if any(configuration._cc_is_workflow() for configuration in self):
            require_cc_administrator(self.env)
        return super().unlink()

    @api.constrains("cc_send_enabled", "cc_execution_user_id", "company_id", "model_id")
    def _check_cc_configuration(self):
        for configuration in self.filtered("cc_send_enabled"):
            if (
                configuration.model != "crm.lead"
                or not configuration.company_id
                or not configuration.cc_execution_user_id.active
                or configuration.cc_execution_user_id.share
                or configuration.company_id
                not in configuration.cc_execution_user_id.company_ids
            ):
                raise ValidationError(
                    _(
                        "Choose CRM leads, a company and an active execution user "
                        "from that company."
                    )
                )

    @api.depends(
        "filter_id.domain",
        "filter_id",
        "editable_domain",
        "cc_entry_after",
        "cc_lead_entry",
        "automation_step_ids.step_type",
        "cc_entry_lead_id",
    )
    def _compute_domain(self):
        result = super()._compute_domain()
        for configuration in self:
            if configuration._cc_is_workflow() and configuration.cc_entry_after:
                configuration.domain = repr(
                    expression.AND(
                        [
                            safe_eval(
                                configuration.domain, configuration._get_eval_context()
                            ),
                            [
                                ("type", "=", "lead"),
                                (
                                    "create_date",
                                    ">=",
                                    fields.Datetime.to_string(
                                        configuration.cc_entry_after
                                    ),
                                ),
                                ("id", ">", configuration.cc_entry_lead_id),
                            ],
                        ]
                    )
                )
        return result

    def _get_automation_records_to_create(self):
        self.ensure_one()
        if not self._cc_is_workflow():
            return super()._get_automation_records_to_create()
        # Both OCA's periodic cron and the prompt create trigger use this gate.
        if not self.cc_send_enabled or not self.cc_entry_after:
            return self.env[self.model]
        self._check_cc_configuration()
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM automation_configuration WHERE id = %s FOR UPDATE",
            [self.id],
        )
        # Fence concurrent enrollments under Odoo's REPEATABLE READ isolation.
        self.env.cr.execute(
            "UPDATE automation_configuration SET write_date = write_date WHERE id = %s",
            [self.id],
        )
        records = super()._get_automation_records_to_create()
        selected = self.env.context.get("cc_enrollment_lead_ids")
        if selected is not None:
            records = records.filtered(lambda lead: lead.id in selected)
        return (
            records.with_user(self.cc_execution_user_id)
            .with_company(self.company_id)
            .with_context(allowed_company_ids=self.company_id.ids)
            .search([("id", "in", records.ids)])
        )

    def _job_cc_enroll_lead(self, lead_id):
        self.ensure_one()
        if not self.active or self.state != "periodic" or not self.cc_lead_entry:
            return
        self.with_context(cc_enrollment_lead_ids=[lead_id]).run_automation()


class AutomationRecord(models.Model):
    _inherit = "automation.record"

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            configuration = self.env["automation.configuration"].browse(
                values.get("configuration_id")
            )
            if configuration and configuration._cc_is_workflow():
                require_cc_administrator(self.env)
        result = super().create(vals_list)
        if any(record.configuration_id._cc_is_workflow() for record in result):
            require_cc_administrator(self.env)
        return result

    def write(self, values):
        configurations = self.mapped("configuration_id")
        if values.get("configuration_id"):
            configurations |= self.env["automation.configuration"].browse(
                values["configuration_id"]
            )
        if any(configuration._cc_is_workflow() for configuration in configurations):
            require_cc_administrator(self.env)
        return super().write(values)

    def unlink(self):
        if any(record.configuration_id._cc_is_workflow() for record in self):
            require_cc_administrator(self.env)
        return super().unlink()

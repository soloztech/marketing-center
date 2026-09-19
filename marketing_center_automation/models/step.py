import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.osv import expression

from .configuration import require_cc_administrator

CC_RECEIPT_TOKEN = object()
CC_RECEIPT_FIELDS = {"cc_request_uuid", "cc_channel_id", "cc_message_id", "cc_status"}


class AutomationConfigurationStep(models.Model):
    _inherit = "automation.configuration.step"

    step_type = fields.Selection(
        selection_add=[("contact_center", "Mensagem no Contact Center")],
        ondelete={"contact_center": "cascade"},
    )
    cc_account_id = fields.Many2one("contact.center.account", string="Caixa de entrada")
    cc_body = fields.Text(
        string="Mensagem", help="Variáveis: {{nome}}, {{lead}}, {{empresa}}."
    )
    cc_responsible_id = fields.Many2one(
        "res.users",
        string="Responsável pelo lead",
        domain=[("share", "=", False)],
        help="Preenche somente quando o lead ainda não tem responsável.",
    )
    cc_stop_on_reply = fields.Boolean(
        string="Parar se o cliente respondeu", default=True
    )
    cc_stop_on_human = fields.Boolean(
        string="Parar se a equipe iniciou contato", default=True
    )

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            configuration = self.env["automation.configuration"].browse(
                values.get("configuration_id")
            )
            if values.get("step_type") == "contact_center" or (
                configuration and configuration._cc_is_workflow()
            ):
                require_cc_administrator(self.env)
        result = super().create(vals_list)
        if any(step.configuration_id._cc_is_workflow() for step in result):
            require_cc_administrator(self.env)
        return result

    def write(self, values):
        target_configuration = self.env["automation.configuration"].browse(
            values.get("configuration_id")
        )
        if (
            values.get("step_type") == "contact_center"
            or (target_configuration and target_configuration._cc_is_workflow())
            or any(step.configuration_id._cc_is_workflow() for step in self)
        ):
            require_cc_administrator(self.env)
        return super().write(values)

    def unlink(self):
        if any(step.configuration_id._cc_is_workflow() for step in self):
            require_cc_administrator(self.env)
        return super().unlink()

    @api.model
    def _step_icons(self):
        return {**super()._step_icons(), "contact_center": "fa fa-comments"}

    @api.constrains(
        "step_type", "cc_account_id", "configuration_id", "cc_responsible_id"
    )
    def _check_cc_step(self):
        for step in self.filtered(lambda record: record.step_type == "contact_center"):
            if step.configuration_id.model != "crm.lead":
                raise ValidationError(
                    _("Contact Center message steps require CRM leads.")
                )
            company = step.configuration_id.company_id
            if step.cc_account_id and step.cc_account_id.company_id != company:
                raise ValidationError(
                    _("The inbox and workflow must belong to the same company.")
                )
            if (
                step.cc_responsible_id
                and company not in step.cc_responsible_id.company_ids
            ):
                raise ValidationError(
                    _("The responsible user must belong to the workflow company.")
                )


class AutomationRecordStep(models.Model):
    _inherit = "automation.record.step"

    cc_request_uuid = fields.Char(
        default=lambda self: str(uuid.uuid4()), readonly=True, copy=False, index=True
    )
    cc_channel_id = fields.Many2one("mail.channel", readonly=True, copy=False)
    cc_message_id = fields.Many2one("mail.message", readonly=True, copy=False)
    cc_status = fields.Selection(
        [
            ("queued", "Na fila"),
            ("submitted", "Enfileirada no Contact Center"),
            ("simulated", "Simulada"),
            ("disabled", "Envios desativados"),
            ("stopped", "Interrompida pelo atendimento"),
        ],
        readonly=True,
        copy=False,
    )

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for values in vals_list:
            if CC_RECEIPT_FIELDS.intersection(values):
                raise AccessError(
                    _("Message receipts are managed by the automation service.")
                )
            configuration_step = self.env["automation.configuration.step"].browse(
                values.get("configuration_step_id")
            )
            if (
                configuration_step
                and configuration_step.configuration_id._cc_is_workflow()
            ):
                require_cc_administrator(self.env)
            # Ignore caller-controlled default_cc_request_uuid as well.
            prepared.append({**values, "cc_request_uuid": str(uuid.uuid4())})
        result = super().create(prepared)
        if any(record.configuration_id._cc_is_workflow() for record in result):
            require_cc_administrator(self.env)
        return result

    def write(self, values):
        if CC_RECEIPT_FIELDS.intersection(values) and (
            self.env.context.get("cc_receipt_service") is not CC_RECEIPT_TOKEN
        ):
            raise AccessError(
                _("Message receipts are managed by the automation service.")
            )
        if any(record.configuration_id._cc_is_workflow() for record in self):
            require_cc_administrator(self.env)
        if {"configuration_step_id", "record_id"}.intersection(values):
            configurations = self.mapped("configuration_id")
            if values.get("configuration_step_id"):
                configurations |= (
                    self.env["automation.configuration.step"]
                    .browse(values["configuration_step_id"])
                    .configuration_id
                )
            if values.get("record_id"):
                configurations |= (
                    self.env["automation.record"]
                    .browse(values["record_id"])
                    .configuration_id
                )
            if any(configuration._cc_is_workflow() for configuration in configurations):
                require_cc_administrator(self.env)
        return super().write(values)

    def unlink(self):
        if any(record.configuration_id._cc_is_workflow() for record in self):
            require_cc_administrator(self.env)
        return super().unlink()

    def _cc_receipt_write(self, values):
        return self.with_context(cc_receipt_service=CC_RECEIPT_TOKEN).write(values)

    def run(self, trigger_activity=True):
        self.ensure_one()
        if self.step_type != "contact_center" or self.is_test:
            return super().run(trigger_activity=trigger_activity)
        require_cc_administrator(self.env)
        if self.state != "scheduled":
            return self.browse()
        if not self.configuration_id.cc_send_enabled:
            self._cc_receipt_write({"cc_status": "disabled"})
            self._reject()
            return self.browse()
        self._cc_receipt_write({"cc_status": "queued"})
        self.with_delay(
            identity_key="marketing_automation:message:%s" % self.id,
            description="Marketing: Contact Center step %s" % self.id,
        )._job_cc_execute()
        return self.browse()

    def _job_cc_execute(self):
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM automation_record_step WHERE id = %s FOR UPDATE", [self.id]
        )
        self.invalidate_recordset()
        if self.state != "scheduled":
            return
        if (
            not self.configuration_id.active
            or self.configuration_id.state not in ("periodic", "ondemand")
            or not self.configuration_id.cc_send_enabled
        ):
            self._cc_receipt_write({"cc_status": "disabled"})
            self._reject()
            return
        # OCA owns completion, errors, child steps and retry. The job only moves
        # transport admission outside the CRM / cron transaction.
        super().run()

    def _cc_has_human_contact(self, lead, channels):
        step = self.configuration_step_id
        if not channels or not (step.cc_stop_on_reply or step.cc_stop_on_human):
            return False
        domain = [
            ("channel_binding_id.channel_id", "in", channels.ids),
            ("message_id.date", ">=", lead.create_date),
            ("content_type", "not in", ["reaction", "revoke", "protocol", "receipt"]),
        ]
        alternatives = []
        if step.cc_stop_on_reply:
            alternatives.append(
                [("direction", "=", "inbound"), ("origin", "=", "provider")]
            )
        if step.cc_stop_on_human:
            alternatives.append(
                [
                    ("direction", "=", "outbound"),
                    ("origin", "in", ["agent", "external_device"]),
                ]
            )
        # Channels have already passed the execution user's CC visibility check.
        return bool(
            self.env["contact.center.message.binding"]
            .sudo()
            .search_count(expression.AND([domain, expression.OR(alternatives)]))
        )

    def _run_contact_center(self):
        if self.is_test:
            self._cc_receipt_write({"cc_status": "simulated"})
            return True
        configuration = self.configuration_id
        if not configuration.cc_send_enabled:
            self._cc_receipt_write({"cc_status": "disabled"})
            return False
        configuration._check_cc_configuration()
        step = self.configuration_step_id
        step._check_cc_step()
        if not step.cc_account_id or not (step.cc_body or "").strip():
            raise ValidationError(
                _("Choose an inbox and enter a message before sending.")
            )
        lead = (
            self.record_id.resource_ref.with_user(configuration.cc_execution_user_id)
            .with_company(configuration.company_id)
            .with_context(allowed_company_ids=configuration.company_id.ids)
        )
        lead.check_access_rights("write")
        lead.check_access_rule("write")
        if lead.company_id != configuration.company_id:
            raise ValidationError(
                _("The lead and workflow must belong to the same company.")
            )
        if (
            lead.cc_automation_paused
            or not lead.active
            or lead.type != "lead"
            or not configuration.cc_entry_after
            or lead.create_date < configuration.cc_entry_after
            or lead.id <= configuration.cc_entry_lead_id
            or self._cc_has_human_contact(lead, lead._visible_contact_center_channels())
        ):
            self._cc_receipt_write({"cc_status": "stopped"})
            return False
        if self.cc_message_id:
            # OCA may have failed while creating child steps after admission.
            # Retrying must not open a second conversation if the phone changed.
            return True
        # OCA catches errors. Savepoint keeps a failed admission from leaving a
        # half-created CRM link or receipt behind; the fixed UUID survives retries.
        with self.env.cr.savepoint():
            channel = lead._contact_center_start_and_link(step.cc_account_id)
            lead._contact_center_lock_conversation_graph(channel_ids=channel.ids)
            if self._cc_has_human_contact(lead, channel):
                self._cc_receipt_write(
                    {"cc_status": "stopped", "cc_channel_id": channel.id}
                )
                return False
            body = step.cc_body
            for key, value in {
                "nome": lead.contact_name or lead.partner_name or lead.name,
                "lead": lead.name,
                "empresa": lead.partner_name or "",
            }.items():
                body = body.replace("{{%s}}" % key, value or "")
            response = lead.env["contact.center.ui.api"]._send_automation_message(
                channel.id, body, client_request_id=self.cc_request_uuid
            )
            if step.cc_responsible_id and not lead.user_id:
                lead.write({"user_id": step.cc_responsible_id.id})
            self._cc_receipt_write(
                {
                    "cc_channel_id": channel.id,
                    "cc_message_id": response["message_id"],
                    "cc_status": "submitted",
                }
            )
        return True

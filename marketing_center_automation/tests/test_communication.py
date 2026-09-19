import uuid
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import SavepointCase

from odoo.addons.contact_center_base.tests.test_start_conversation import (
    DirectStartTestAdapter,
)
from odoo.addons.queue_job.tests.common import trap_jobs


@tagged("post_install", "-at_install")
class TestCommunicationAutomation(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        groups = cls.env.ref(
            "contact_center_base.group_contact_center_agent"
        ) | cls.env.ref("sales_team.group_sale_salesman_all_leads")
        cls.agent = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Communication executor",
                    "login": "communication-executor-%s" % uuid.uuid4(),
                    "active": True,
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, groups.ids)],
                }
            )
        )
        cls.configuration = cls.env["automation.configuration"].create(
            {
                "name": "Communication test",
                "model_id": cls.env.ref("crm.model_crm_lead").id,
                "company_id": cls.env.company.id,
                "cc_lead_entry": True,
                "cc_execution_user_id": cls.agent.id,
                "is_periodic": True,
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Automation test inbox",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "automation-test-%s" % uuid.uuid4(),
                "access_user_ids": [(6, 0, cls.agent.ids)],
            }
        )
        cls.step = cls.env["automation.configuration.step"].create(
            {
                "configuration_id": cls.configuration.id,
                "name": "Welcome",
                "step_type": "contact_center",
                "cc_account_id": cls.account.id,
                "cc_body": "Olá, {{nome}}!",
                "trigger_interval": -1,
            }
        )
        cls.configuration.start_automation()
        cls.channel = cls.env["mail.channel"].create(
            {"name": "Automation mock channel"}
        )
        cls.message = cls.env["mail.message"].create({"body": "Automation mock"})

    def _lead(self, **values):
        # SavepointCase otherwise retains the first transaction timestamp even
        # after the workflow was enabled. Model a new request's audit timestamp.
        with patch.object(type(self.env.cr), "now", return_value=fields.Datetime.now()):
            return self.env["crm.lead"].create(
                {
                    "name": "New lead",
                    "contact_name": "Maria",
                    "type": "lead",
                    "company_id": self.env.company.id,
                    "phone": "+5511998765432",
                    "user_id": self.agent.id,
                    **values,
                }
            )

    def _ready(self):
        self.configuration.cc_send_enabled = True
        with trap_jobs():
            lead = self._lead()
        record = self.configuration._create_record(lead)
        return lead, record.automation_step_ids

    def test_disabled_has_no_enrollment_or_transport(self):
        self.assertFalse(self.configuration.cc_send_enabled)
        with trap_jobs() as trap:
            self._lead()
            self.configuration.run_automation()
            trap.assert_jobs_count(0)
        self.assertFalse(
            self.env["automation.record"].search(
                [("configuration_id", "=", self.configuration.id)]
            )
        )

    def test_leads_menu_opens_native_lead_list_with_form_fallback(self):
        action = self.env.ref("marketing_center_crm.action_marketing_leads")
        native_tree = self.env.ref("crm.crm_case_tree_view_leads")
        self.assertEqual(action.views[0], (native_tree.id, "tree"))
        self.assertIn((False, "form"), action.views)

    def test_create_only_queues_entry(self):
        self.configuration.cc_send_enabled = True
        with trap_jobs() as trap, patch.object(
            type(self.env["contact.center.ui.api"]), "_send_automation_message"
        ) as send:
            lead = self._lead()
            trap.assert_jobs_count(1, only=self.configuration._job_cc_enroll_lead)
            send.assert_not_called()
        self.assertEqual(lead.type, "lead")

    def test_periodic_enrollment_excludes_history_and_is_repeatable(self):
        historical = self._lead()
        self.configuration.cc_send_enabled = True
        with trap_jobs():
            lead = self._lead()
            self.configuration.run_automation()
            self.configuration.run_automation()
        records = self.env["automation.record"].search(
            [("configuration_id", "=", self.configuration.id)]
        )
        self.assertEqual(records.mapped("res_id"), lead.ids)
        self.assertNotIn(historical.id, records.mapped("res_id"))

    def test_test_mode_never_starts_or_sends(self):
        lead = self._lead()
        record = self.configuration._create_record(lead, is_test=True)
        with patch.object(
            type(lead), "_contact_center_start_and_link"
        ) as start, patch.object(
            type(self.env["contact.center.ui.api"]), "_send_automation_message"
        ) as send:
            record.automation_step_ids.run()
            start.assert_not_called()
            send.assert_not_called()
        self.assertEqual(record.automation_step_ids.cc_status, "simulated")
        self.assertEqual(record.automation_step_ids.state, "done")

    def test_replay_admits_only_one_message_and_uses_fixed_uuid(self):
        lead, step = self._ready()
        original_uuid = step.cc_request_uuid
        with patch.object(
            type(lead), "_contact_center_start_and_link", return_value=self.channel
        ), patch.object(
            type(lead),
            "_visible_contact_center_channels",
            return_value=self.env["mail.channel"],
        ), patch.object(
            type(step), "_cc_has_human_contact", return_value=False
        ), patch.object(
            type(self.env["contact.center.ui.api"]),
            "_send_automation_message",
            return_value={"message_id": self.message.id, "state": "pending"},
        ) as send:
            step._job_cc_execute()
            step._job_cc_execute()
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.kwargs["client_request_id"], original_uuid)
            self.assertEqual(send.call_args.args[1], "Olá, Maria!")
        self.assertEqual(step.cc_message_id, self.message)
        self.assertEqual(step.state, "done")

    def test_failure_then_manual_retry_keeps_uuid(self):
        lead, step = self._ready()
        original_uuid = step.cc_request_uuid
        with patch.object(
            type(lead), "_contact_center_start_and_link", return_value=self.channel
        ), patch.object(
            type(lead),
            "_visible_contact_center_channels",
            return_value=self.env["mail.channel"],
        ), patch.object(
            type(step), "_cc_has_human_contact", return_value=False
        ), patch.object(
            type(self.env["contact.center.ui.api"]),
            "_send_automation_message",
            side_effect=ValidationError("Simulated admission failure"),
        ):
            step._job_cc_execute()
        self.assertEqual(step.state, "error")
        self.assertFalse(step.cc_message_id)
        step.retry()
        self.assertEqual(step.cc_request_uuid, original_uuid)
        self.assertEqual(step.state, "scheduled")

    def test_human_contact_stops_before_opening_conversation(self):
        lead, step = self._ready()
        with patch.object(
            type(lead), "_visible_contact_center_channels", return_value=self.channel
        ), patch.object(
            type(step), "_cc_has_human_contact", return_value=True
        ), patch.object(
            type(lead), "_contact_center_start_and_link"
        ) as start:
            step._job_cc_execute()
            start.assert_not_called()
        self.assertEqual(step.cc_status, "stopped")
        self.assertFalse(step.child_ids)

    def test_manual_lead_pause_stops_before_transport(self):
        lead, step = self._ready()
        lead.cc_automation_paused = True
        with patch.object(type(lead), "_contact_center_start_and_link") as start:
            step._job_cc_execute()
            start.assert_not_called()
        self.assertEqual(step.cc_status, "stopped")

    def test_disabled_after_enqueue_rechecks_gate(self):
        lead, step = self._ready()
        self.configuration.cc_send_enabled = False
        with patch.object(type(lead), "_contact_center_start_and_link") as start:
            step._job_cc_execute()
            start.assert_not_called()
        self.assertEqual(step.cc_status, "disabled")

    def test_reactivation_does_not_release_old_pending_steps(self):
        lead, step = self._ready()
        self.configuration.cc_send_enabled = False
        self.configuration.cc_send_enabled = True
        with patch.object(type(lead), "_contact_center_start_and_link") as start:
            step._job_cc_execute()
            start.assert_not_called()
        self.assertEqual(step.state, "rejected")
        self.assertFalse(step.cc_message_id)

    def test_wrong_company_inbox_is_rejected(self):
        company = self.env["res.company"].create({"name": "Other automation company"})
        account = self.env["contact.center.account"].create(
            {
                "name": "Other automation inbox",
                "company_id": company.id,
                "platform": "whatsapp",
                "external_ref": str(uuid.uuid4()),
            }
        )
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.step.cc_account_id = account

    def test_receipt_cannot_be_forged_by_rpc_write(self):
        _lead, step = self._ready()
        with self.assertRaises(AccessError):
            step.write({"cc_request_uuid": str(uuid.uuid4())})
        with self.assertRaises(AccessError):
            step.write({"cc_message_id": self.message.id})

    def test_manager_cannot_choose_privileged_executor_or_edit_message(self):
        manager = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Automation manager",
                    "login": "automation-manager-%s" % uuid.uuid4(),
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [
                        (
                            6,
                            0,
                            self.env.ref("automation_oca.group_automation_manager").ids,
                        )
                    ],
                }
            )
        )
        with self.assertRaises(AccessError):
            self.configuration.with_user(manager).write(
                {"cc_execution_user_id": self.env.user.id}
            )
        with self.assertRaises(AccessError):
            self.step.with_user(manager).write({"cc_body": "Changed"})
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            self.env["automation.configuration"].with_user(manager).with_context(
                default_cc_lead_entry=True,
                default_model_id=self.env.ref("crm.model_crm_lead").id,
            ).create({"name": "Context defaults bypass"})
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            self.env["automation.configuration.step"].with_user(manager).with_context(
                default_configuration_id=self.configuration.id,
                default_step_type="contact_center",
            ).create(
                {"name": "Context step bypass", "mail_author_id": manager.partner_id.id}
            )
        lead, step = self._ready()
        with self.assertRaises(AccessError):
            step.with_user(manager).write({"state": "scheduled"})
        with self.assertRaises(AccessError):
            step.with_user(manager).unlink()
        with self.assertRaises(AccessError):
            step.record_id.with_user(manager).unlink()
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            self.env["automation.record"].with_user(manager).with_context(
                default_configuration_id=self.configuration.id,
            ).create({"model": "crm.lead", "res_id": lead.id})

    def test_real_workflow_links_crm_and_admits_one_automation_outbox(self):
        self.env.company.country_id = self.env.ref("base.br")
        self.env["contact.center.provider.connection"].create(
            {
                "name": "Automation fake connection",
                "account_id": self.account.id,
                "adapter_key": "test.direct_start",
                "external_ref": str(uuid.uuid4()),
                "provider_schema_version": "fixture-v1",
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": True,
                "capabilities_json": {"send_message": True},
                "last_state_observed_at": fields.Datetime.now(),
                "last_state_source": "health_job",
                "health_detail": "healthy",
            }
        )
        self.configuration.cc_send_enabled = True
        with trap_jobs(), patch.object(
            DirectStartTestAdapter,
            "execute_command",
            side_effect=AssertionError("The workflow must only enqueue transport"),
        ) as transport:
            lead = self._lead()
            self.configuration._job_cc_enroll_lead(lead.id)
            record = self.env["automation.record"].search(
                [
                    ("configuration_id", "=", self.configuration.id),
                    ("res_id", "=", lead.id),
                ]
            )
            self.assertEqual(len(record), 1)
            step = record.automation_step_ids
            step._job_cc_execute()
            self.assertEqual(step.state, "done", step.error_trace)
            self.assertEqual(step.cc_status, "submitted")
            step._job_cc_execute()
            transport.assert_not_called()
        self.assertFalse(lead.partner_id)
        self.assertEqual(
            lead._conversation_links().channel_id.id, step.cc_channel_id.id
        )
        binding = self.env["contact.center.message.binding"].search(
            [("message_id", "=", step.cc_message_id.id)]
        )
        self.assertEqual(len(binding), 1)
        self.assertEqual(binding.origin, "automation")
        self.assertEqual(
            self.env["contact.center.outbox.command"].search_count(
                [("channel_binding_id", "=", binding.channel_binding_id.id)]
            ),
            1,
        )

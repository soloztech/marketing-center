import uuid
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.contact_center_crm.models.intake_policy import INTAKE_TOKEN
from odoo.addons.queue_job.tests.common import trap_jobs

from .test_communication import CommunicationAutomationCase


class TestIntakeAutomationGuard(CommunicationAutomationCase):
    def _intake(self, **values):
        enrichment = (
            {"iap_enrich_done": True}
            if "iap_enrich_done" in self.env["crm.lead"]._fields
            else {}
        )
        with patch.object(type(self.env.cr), "now", return_value=fields.Datetime.now()):
            return (
                self.env["crm.lead"]
                .with_context(
                    crm_intake_service=INTAKE_TOKEN,
                    mail_create_nolog=True,
                    mail_create_nosubscribe=True,
                    mail_auto_subscribe_no_notify=True,
                )
                .create(
                    {
                        "name": "Synthetic intake",
                        "company_id": self.env.company.id,
                        "user_id": self.agent.id,
                        "type": "lead",
                        "email_from": "synthetic@fixture.invalid",
                        "contact_center_intake_created": True,
                        **enrichment,
                        **values,
                    }
                )
            )

    def test_intake_has_no_entry_or_periodic_enrollment(self):
        self.configuration.cc_send_enabled = True
        with trap_jobs() as trap:
            lead = self._intake()
            control = self._lead()
            lead._cc_queue_automation_entry()
            self.configuration.run_automation()
            trap.assert_jobs_count(1, only=self.configuration._job_cc_enroll_lead)
        self.assertFalse(
            self.env["automation.record"].search(
                [("res_id", "=", lead.id), ("model", "=", "crm.lead")]
            )
        )
        self.assertEqual(
            self.env["automation.record"]
            .search(
                [
                    ("configuration_id", "=", self.configuration.id),
                    ("model", "=", "crm.lead"),
                ]
            )
            .mapped("res_id"),
            control.ids,
        )
        ordinary = self.env["automation.configuration"].create(
            {
                "name": "Non communication periodic",
                "model_id": self.env.ref("crm.model_crm_lead").id,
                "company_id": self.env.company.id,
                "is_periodic": True,
            }
        )
        self.env["automation.configuration.step"].create(
            {
                "name": "Native action",
                "configuration_id": ordinary.id,
                "step_type": "action",
            }
        )
        ordinary.start_automation()
        self.assertNotIn(lead, ordinary._get_automation_records_to_create())
        self.assertIn(control, ordinary._get_automation_records_to_create())

    def test_all_step_types_reject_even_explicit_manual_enrollment(self):
        self.configuration.cc_send_enabled = True
        intake = self._intake()
        with self.assertRaises(ValidationError):
            self.configuration._create_record(intake)
        with self.assertRaises(ValidationError):
            self.env["automation.record"].create(
                self.configuration._create_record_vals(intake)
            )
        with trap_jobs():
            ordinary = self._lead()
        enrollment = self.configuration._create_record(ordinary)
        with self.assertRaises(ValidationError):
            enrollment.write({"res_id": intake.id})
        enrollment.write({"res_id": ordinary.id})
        enrollment.unlink()
        template = self.env["mail.template"].create(
            {
                "name": "Must not mail",
                "model_id": self.env.ref("crm.model_crm_lead").id,
                "subject": "Not authorized",
                "body_html": "Synthetic",
                "email_to": "synthetic@fixture.invalid",
            }
        )
        action = self.env["ir.actions.server"].create(
            {
                "name": "Must not execute",
                "model_id": self.env.ref("crm.model_crm_lead").id,
                "state": "code",
                "code": "record.write({'description': 'UNAUTHORIZED ACTION'})",
            }
        )
        for step_type, entry in [
            (kind, entry)
            for kind in ("mail", "action", "activity", "contact_center")
            for entry in ("run", "_job_cc_execute")
        ]:
            with self.subTest(step_type=step_type, entry=entry):
                self.step.write(
                    {
                        "step_type": step_type,
                        "mail_template_id": template.id,
                        "server_action_id": action.id,
                    }
                )
                lead = self._lead()
                record = self.configuration._create_record(lead)
                lead.with_context(crm_intake_service=INTAKE_TOKEN).write(
                    {"contact_center_intake_created": True}
                )
                steps = record.automation_step_ids
                mail_before = self.env["mail.mail"].search_count([])
                out_before = self.env["contact.center.outbox.command"].search_count([])
                with patch.object(
                    type(lead), "_contact_center_start_and_link"
                ) as start, patch.object(
                    type(self.env["contact.center.ui.api"]), "_send_automation_message"
                ) as send:
                    getattr(steps, entry)()
                    start.assert_not_called()
                    send.assert_not_called()
                self.assertEqual(steps.state, "rejected")
                self.assertNotIn("UNAUTHORIZED ACTION", lead.description or "")
                self.assertEqual(self.env["mail.mail"].search_count([]), mail_before)
                self.assertEqual(
                    self.env["contact.center.outbox.command"].search_count([]),
                    out_before,
                )
                record.unlink()

    def test_native_create_and_write_actions_skip_only_intake(self):
        for trigger in ("on_create", "on_write"):
            self.env["base.automation"].create(
                {
                    "name": "Native guard proof",
                    "model_id": self.env.ref("crm.model_crm_lead").id,
                    "trigger": trigger,
                    "state": "code",
                    "code": "record.write({'description': 'NATIVE AUTOMATION'})",
                }
            )
        with trap_jobs():
            lead = self._intake()
            ordinary = self._lead()
        self.assertFalse(lead.description)
        self.assertIn("NATIVE AUTOMATION", ordinary.description)
        lead.name = "Human qualification keeps exclusion"
        self.assertFalse(lead.description)
        self.assertTrue(lead.contact_center_intake_created)

    def test_direct_contact_step_stops_intake_and_control_reaches_transport(self):
        self.configuration.cc_send_enabled = True
        with trap_jobs():
            lead = self._lead()
        record = self.configuration._create_record(lead)
        step = record.automation_step_ids
        with patch.object(
            type(lead), "_contact_center_start_and_link", return_value=self.channel
        ) as start, patch.object(
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
            self.assertTrue(step._run_contact_center())
            start.assert_called_once()
            send.assert_called_once()
        # A second, scheduled record becomes intake-marked after enrollment,
        # exactly as a native CRM merge can propagate the permanent marker.
        with trap_jobs():
            lead = self._lead()
        record = self.configuration._create_record(lead)
        step = record.automation_step_ids
        lead.with_context(crm_intake_service=INTAKE_TOKEN).write(
            {"contact_center_intake_created": True}
        )
        out_before = self.env["contact.center.outbox.command"].search_count([])
        with patch.object(
            type(lead), "_contact_center_start_and_link"
        ) as start, patch.object(
            type(self.env["contact.center.ui.api"]), "_send_automation_message"
        ) as send:
            self.assertFalse(step._run_contact_center())
            start.assert_not_called()
            send.assert_not_called()
        self.assertEqual(step.cc_status, "stopped")
        self.assertEqual(
            self.env["contact.center.outbox.command"].search_count([]), out_before
        )

    def test_dedup_grouping_keeps_ordinary_shared_and_null_keys(self):
        field = self.env["ir.model.fields"]._get("crm.lead", "email_from")
        for value in ("dedup@fixture.invalid", False):
            with self.subTest(value=value), trap_jobs():
                intake = self._intake(email_from=value)
                ordinary = self._lead(email_from=value)
                configuration = self.env["automation.configuration"].create(
                    {
                        "name": "Dedup proof",
                        "model_id": self.env.ref("crm.model_crm_lead").id,
                        "company_id": self.env.company.id,
                        "is_periodic": True,
                        "field_id": field.id,
                        "editable_domain": repr(
                            [("id", "in", [intake.id, ordinary.id])]
                        ),
                    }
                )
                self.env["automation.configuration.step"].create(
                    {
                        "name": "Action",
                        "configuration_id": configuration.id,
                        "step_type": "action",
                    }
                )
                configuration.start_automation()
                self.assertEqual(
                    configuration._get_automation_records_to_create(), ordinary
                )
                configuration.run_automation()
                self.assertEqual(
                    self.env["automation.record"]
                    .search([("configuration_id", "=", configuration.id)])
                    .mapped("res_id"),
                    ordinary.ids,
                )

    def test_completed_step_history_survives_later_intake_merge(self):
        self.configuration.cc_send_enabled = True
        for state in ("done", "error", "cancel", "expired"):
            with self.subTest(state=state), trap_jobs():
                lead = self._lead()
                record = self.configuration._create_record(lead)
                step = record.automation_step_ids
                step.write({"state": state})
                before = step.read(["state", "processed_on"])[0]
                lead.with_context(crm_intake_service=INTAKE_TOKEN).write(
                    {"contact_center_intake_created": True}
                )
                step.run()
                step._job_cc_execute()
                self.assertEqual(step.read(["state", "processed_on"])[0], before)

    def test_native_onchange_skips_intake_and_preserves_ordinary_action(self):
        rule = self.env["base.automation"].create(
            {
                "name": "Native onchange proof",
                "model_id": self.env.ref("crm.model_crm_lead").id,
                "trigger": "on_change",
                "on_change_field_ids": [
                    (6, 0, self.env["ir.model.fields"]._get("crm.lead", "name").ids)
                ],
                "state": "code",
                "code": "action = {'value': {'description': 'ONCHANGE ACTION'}}",
            }
        )
        with trap_jobs():
            intake = self._intake()
            ordinary = self._lead()
        # Run the actual registered native onchange callback on pseudo-records.
        for lead, expected in ((intake, False), (ordinary, "ONCHANGE ACTION")):
            pseudo = lead.new({"name": "Changed"}, origin=lead)
            pseudo._onchange_eval("name", "1", {"value": {}, "warnings": set()})
            if expected:
                self.assertIn(expected, pseudo.description)
            else:
                self.assertFalse(pseudo.description)
        self.assertTrue(rule.active)

    def test_no_automation_access_cannot_probe_intake_provenance(self):
        with trap_jobs():
            intake = self._intake()
            ordinary = self._lead()
        user = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "No automation access",
                    "login": "intake-access-oracle-proof",
                    "groups_id": [(6, 0, self.env.ref("base.group_user").ids)],
                }
            )
        )
        for lead in (intake, ordinary):
            with self.subTest(lead=lead.id), self.assertRaises(AccessError):
                self.env["automation.record"].with_user(user).create(
                    {"model": "crm.lead", "res_id": lead.id}
                )

    def _ordinary_configuration(self, **values):
        return self.env["automation.configuration"].create(
            {
                "name": "Ordinary configuration",
                "model_id": self.env.ref("crm.model_crm_lead").id,
                "company_id": self.env.company.id,
                **values,
            }
        )

    def test_manager_without_crm_cannot_probe_create_write_or_test_wizard(self):
        manager = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Automation manager without CRM",
                    "login": uuid.uuid4().hex,
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
        Model = self.env["automation.record"].with_user(manager)
        self.assertTrue(Model.check_access_rights("create", raise_exception=False))
        self.assertTrue(Model.check_access_rights("write", raise_exception=False))
        self.assertFalse(
            self.env["crm.lead"]
            .with_user(manager)
            .check_access_rights("read", raise_exception=False)
        )
        with trap_jobs():
            intake, ordinary = self._intake(), self._lead()
        config = self._ordinary_configuration()
        existing = config._create_record(ordinary)
        for lead in (intake, ordinary):
            with self.subTest(lead=lead.id):
                with self.assertRaises(AccessError), self.env.cr.savepoint():
                    Model.create(config._create_record_vals(lead))
                with self.assertRaises(AccessError), self.env.cr.savepoint():
                    existing.with_user(manager).write({"res_id": lead.id})
                with self.assertRaises(AccessError), self.env.cr.savepoint():
                    wizard = (
                        self.env["automation.configuration.test"]
                        .with_user(manager)
                        .create(
                            {
                                "configuration_id": config.id,
                                "resource_ref": "crm.lead,%s" % lead.id,
                            }
                        )
                    )
                    wizard.test_record()

    def test_string_reference_is_normalized_before_intake_check(self):
        with trap_jobs():
            intake, ordinary = self._intake(), self._lead()
        config = self._ordinary_configuration()
        values = config._create_record_vals(intake)
        values["res_id"] = str(intake.id)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env["automation.record"].create(values)
        values = config._create_record_vals(ordinary)
        values["res_id"] = str(ordinary.id)
        record = self.env["automation.record"].create(values)
        self.assertEqual(record.res_id, ordinary.id)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            record.write({"res_id": str(intake.id)})
        self.assertEqual(record.res_id, ordinary.id)

    def test_export_preserves_authored_dynamic_domain_without_execution_leaf(self):
        domain = (
            "[('create_date', '>=', "
            "datetime.datetime.now() - datetime.timedelta(days=30))]"
        )
        config = self._ordinary_configuration(editable_domain=domain)
        self.assertIn("contact_center_intake_created", config.domain)
        exported = config._export_configuration()
        self.assertEqual(exported["domain"], domain)
        self.assertNotIn("contact_center_intake_created", exported["domain"])

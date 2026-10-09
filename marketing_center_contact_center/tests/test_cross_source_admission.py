import datetime
import unittest
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged

from odoo.addons.contact_center_crm.tests.test_intake import CrmIntakeCase
from odoo.addons.marketing_center_base.models.attribution import (
    ATTRIBUTION_ERASURE_TOKEN,
)
from odoo.addons.marketing_center_base.models.crm.dedup_policy import (
    CrmDedupUnavailable,
)
from odoo.addons.queue_job.tests.common import trap_jobs

from .test_convergence import MarketingContactCenterCrmFixture


@tagged("post_install", "-at_install", "crm_cross_source")
class TestCrossSourceAdmission(CrmIntakeCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if "marketing.center.meta.crm.projection" not in cls.env.registry:
            raise unittest.SkipTest(
                "Optional Meta composition has a separate standalone proof"
            )
        from odoo.addons.marketing_center_meta.tests.test_meta_crm_projection import (
            TestMarketingCenterMetaCrmProjection,
        )

        fixture = TestMarketingCenterMetaCrmProjection
        fixture._setup_meta_crm_fixture.__func__(cls)
        for name in (
            "_new_submission",
            "_authenticate",
            "_touchpoint",
            "_enable_route",
            "_projection_for",
            "_run_projection_job",
        ):
            setattr(cls, name, getattr(fixture, name))

    def setUp(self):
        super().setUp()
        for group in (
            "base.group_system",
            "marketing_center_base.group_marketing_center_admin",
            "contact_center_base.group_contact_center_admin",
        ):
            self.env.user.write({"groups_id": [Command.link(self.env.ref(group).id)]})
        self.agent.write(
            {
                "groups_id": [
                    Command.link(self.env.ref("sales_team.group_sale_manager").id)
                ]
            }
        )
        self.company.write(
            {
                "crm_cross_source_reviewer_id": self.agent.id,
                "crm_cross_source_dedup_enabled": True,
            }
        )
        self._enable_route()
        self.at = max(
            self._after_cutoff(), self.company.crm_cross_source_enabled_at
        ) + datetime.timedelta(seconds=1)

    def _meta(self, phone="+55 11 99876-5432", extra=None):
        with trap_jobs():
            submission = self._new_submission(
                fields={
                    "full_name": ("Synthetic cross-source",),
                    "phone_number": (phone,),
                    **(extra or {}),
                }
            )
            self._authenticate(submission, provider_created_at=self.at)
        return self._projection_for(submission)

    def _whatsapp(self, phone="5511998765432"):
        with patch.object(type(self.env.cr), "now", return_value=self.at):
            binding = self._new(phone=phone)
        with patch.object(fields.Datetime, "now", return_value=self.at):
            self._message(binding, date=self.at)
        return binding

    def _run(self, binding):
        with patch.object(type(self.env.cr), "now", return_value=self.at):
            return super()._run(binding)

    def _project(self, projection):
        with trap_jobs(), patch.object(type(self.env.cr), "now", return_value=self.at):
            self._run_projection_job(projection)
        projection.invalidate_recordset()
        return projection.lead_id

    def test_whatsapp_then_meta_preserves_business_and_two_receipts(self):
        binding = self._whatsapp()
        lead = self._run(binding)
        lead.write({"name": "Keep this title", "user_id": self.agent.id})
        before = self.env["crm.lead"].search_count([])
        mail_before = self.env["mail.mail"].search_count([])
        projection = self._meta()
        self.assertEqual(self._project(projection), lead)
        self.assertEqual(projection.admission_decision, "reused")
        self.assertEqual((lead.name, lead.user_id), ("Keep this title", self.agent))
        self.assertEqual(self.env["crm.lead"].search_count([]), before)
        self.assertEqual(binding.crm_comparison_exact, projection.comparison_exact)
        self.assertTrue(projection.assertion_id)
        self.assertTrue(projection.signal_activity_id)
        self.assertEqual(self.env["mail.mail"].search_count([]), mail_before)
        signals = projection.admission_signals_json
        self._project(projection)
        self.assertEqual(projection.admission_signals_json, signals)

    def test_meta_then_whatsapp_reuses_without_claiming_auto_period(self):
        projection = self._meta()
        lead = self._project(projection)
        binding = self._whatsapp()
        self.assertEqual(self._run(binding), lead)
        self.assertEqual(binding.crm_intake_state, "reused")
        link = (
            self.env["contact.center.crm.conversation.link"]
            .sudo()
            .search(
                [("channel_id", "=", binding.channel_id.id), ("state", "=", "active")]
            )
        )
        self.assertEqual(link.scope_state, "context")
        self.assertEqual(binding.crm_comparison_exact, projection.comparison_exact)

    def test_br_pair_review_is_resolved_only_for_this_conversation(self):
        projection = self._meta()
        lead = self._project(projection)
        binding = self._whatsapp("551198765432")
        self.assertFalse(self._run(binding))
        self.assertEqual(binding.crm_intake_reason, "phone_variant_review")
        api = self.env["contact.center.ui.api"].with_user(self.agent)
        page = api.get_customer_records(binding.channel_id.id)
        self.assertIn(
            lead.id, [item["id"] for item in page["intake"]["review_candidates"]]
        )
        with self.assertRaises(ValidationError):
            api.link_crm_opportunity(binding.channel_id.id, lead.id)
        with trap_jobs():
            api.resolve_crm_intake_review(
                binding.channel_id.id, binding.crm_intake_revision, lead.id, True
            )
        self.assertEqual(binding.crm_intake_state, "resolved")
        self.assertEqual(binding.crm_intake_lead_snapshot, lead.id)
        decision = (
            self.env["contact.center.crm.review.decision"]
            .sudo()
            .search([("binding_id", "=", binding.id)])
        )
        self.assertEqual(
            (decision.kind, decision.comparison_mode, decision.actor_ref),
            ("identity_confirmation", "br_pair", self.agent.id),
        )
        with trap_jobs():
            api.unlink_crm_opportunity(binding.channel_id.id, lead.id)
        self._run(binding)
        self.assertFalse(
            self.env["contact.center.crm.conversation.link"]
            .sudo()
            .search_count(
                [("channel_id", "=", binding.channel_id.id), ("state", "=", "active")]
            )
        )

    def test_deleted_meta_national_phone_receipt_blocks_recreation(self):
        projection = self._meta("(11) 99876-5432")
        lead = self._project(projection)
        lead.unlink()
        before = self.env["crm.lead"].search_count([])
        binding = self._whatsapp()
        self.assertFalse(self._run(binding))
        self.assertEqual(binding.crm_intake_state, "review")
        self.assertEqual(binding.crm_intake_reason, "receipt_without_target")
        self.assertEqual(self.env["crm.lead"].search_count([]), before)

    def test_closed_whatsapp_coentry_is_review_not_second_lead(self):
        binding = self._whatsapp()
        lead = self._run(binding)
        lead.write({"active": False})
        before = self.env["crm.lead"].with_context(active_test=False).search_count([])
        projection = self._meta()
        self.assertFalse(self._project(projection))
        self.assertEqual(
            (projection.state, projection.review_reason),
            ("review", "closed_business_review"),
        )
        self.assertFalse(projection.assertion_id)
        with trap_jobs():
            projection._enqueue(retry_terminal=True)
            projection._mark_skipped()
            self.route.write({"crm_lead_title_prefix": "Changed prefix"})
        self.assertEqual(projection.state, "review")
        self.assertEqual(
            self.env["crm.lead"].with_context(active_test=False).search_count([]),
            before,
        )

    def test_unverified_paid_submission_remains_a_lead_with_activity(self):
        projection = self._meta("bad", extra={"email": ("valid@example.test",)})
        lead = self._project(projection)
        self.assertTrue(lead)
        self.assertEqual(projection.identity_state, "unverified")
        self.assertTrue(projection.signal_activity_id)

    def test_closed_legacy_without_coentry_receipt_does_not_block_new_demand(self):
        self.env["crm.lead"].create(
            {
                "name": "Synthetic archived legacy business",
                "company_id": self.company.id,
                "phone": "+5511998765432",
                "active": False,
            }
        )
        before = self.env["crm.lead"].with_context(active_test=False).search_count([])
        projection = self._meta()
        lead = self._project(projection)
        self.assertTrue(lead)
        self.assertEqual(projection.state, "done")
        self.assertEqual(
            self.env["crm.lead"].with_context(active_test=False).search_count([]),
            before + 1,
        )

    def test_native_partner_and_existing_link_are_preserved_with_protection(self):
        for linked, phone in ((False, "551188887777"), (True, "551177776666")):
            with self.subTest(linked=linked):
                partner = self.env["res.partner"].create(
                    {"name": "Synthetic explicit customer"}
                )
                lead = self.env["crm.lead"].create(
                    {
                        "name": "Synthetic explicit business",
                        "user_id": False,
                        "company_id": self.company.id,
                        "partner_id": partner.id if not linked else False,
                    }
                )
                with patch.object(type(self.env.cr), "now", return_value=self.at):
                    binding = self._new(
                        partner=partner if not linked else None, phone=phone
                    )
                if linked:
                    with trap_jobs():
                        self.env["contact.center.crm.conversation.link"]._link(
                            binding.channel_id, lead, writer="manual", origin="linked"
                        )
                before = self.env["crm.lead"].search_count([])
                self._message(binding, date=self.at)
                self.assertEqual(self._run(binding), lead)
                self.assertEqual(binding.crm_intake_state, "reused")
                self.assertEqual(self.env["crm.lead"].search_count([]), before)

    def test_open_legacy_reuse_remains_context_despite_old_closed_business(self):
        legacy = self.env["crm.lead"].create(
            {
                "name": "Synthetic old closed",
                "company_id": self.company.id,
                "phone": "+5511998765432",
                "active": False,
            }
        )
        lead = self.env["crm.lead"].create(
            {
                "name": "Synthetic existing demand",
                "user_id": False,
                "company_id": self.company.id,
                "phone": "+5511998765432",
            }
        )
        binding = self._whatsapp()
        self.assertEqual(self._run(binding), lead)
        self.assertEqual(lead._conversation_links().scope_state, "context")
        self.assertNotEqual(lead, legacy)
        projection = self._meta()
        before = self.env["crm.lead"].search_count([])
        self.assertFalse(self._project(projection))
        self.assertEqual(projection.state, "review")
        self.assertEqual(projection.review_reason, "business_scope_review")
        self.assertFalse(projection.assertion_id)
        self.assertEqual(self.env["crm.lead"].search_count([]), before)
        self.assertEqual(lead._conversation_links().scope_state, "context")

    def test_policy_edits_preserve_activation_cohort_and_pending_counts(self):
        lead = self._project(self._meta())
        binding = self._whatsapp("551198765432")
        self._run(binding)
        keys = [
            "crm_cross_source_enabled_at",
            "crm_cross_source_binding_watermark",
            "crm_cross_source_submission_watermark",
            "crm_cross_source_projection_watermark",
        ]
        before = self.company.read(keys)
        revision = self.company.crm_cross_source_revision
        self.company.write({"crm_cross_source_window_hours": 48})
        self.assertEqual(self.company.read(keys), before)
        self.assertEqual(self.company.crm_cross_source_revision, revision + 1)
        service = self.env["marketing.crm.service"]
        self.assertEqual(
            service._crm_cross_source_intake_counts(self.company)["count"], 1
        )
        with trap_jobs():
            self.env["contact.center.ui.api"].with_user(
                self.agent
            ).resolve_crm_intake_review(
                binding.channel_id.id, binding.crm_intake_revision, lead.id, True
            )
        self.assertEqual(
            service._crm_cross_source_intake_counts(self.company)["count"], 0
        )

    def test_activity_falls_back_from_ineligible_seller_without_email(self):
        for archived, phone in ((False, "5511998765432"), (True, "5511988887777")):
            with self.subTest(archived=archived):
                binding = self._whatsapp(phone)
                lead = self._run(binding)
                lead.write({"user_id": self.salesperson.id})
                if archived:
                    self.salesperson.write({"active": False})
                before = self.env["mail.mail"].search_count([])
                projection = self._meta("+" + phone)
                self.assertEqual(self._project(projection), lead)
                self.assertEqual(projection.signal_activity_id.user_id, self.agent)
                self.assertEqual(self.env["mail.mail"].search_count([]), before)
                signals = projection.admission_signals_json
                self._project(projection)
                self.assertEqual(projection.admission_signals_json, signals)

    def test_inaccessible_business_gets_only_generic_inbox_without_leak(self):
        binding = self._whatsapp()
        lead = self._run(binding)
        self.env["ir.rule"].create(
            {
                "name": "Synthetic confidential CRM record",
                "model_id": self.env["ir.model"]._get_id("crm.lead"),
                "domain_force": "[('id', '!=', %s)]" % lead.id,
            }
        )
        before = self.env["mail.mail"].search_count([])
        projection = self._meta()
        self.assertFalse(self._project(projection))
        self.assertEqual(projection.review_reason, "inaccessible")
        self.assertFalse(projection.review_candidate_ids)
        self.assertFalse(projection.signal_activity_id)
        message = projection.signal_message_id
        self.assertTrue(message)
        self.assertFalse(message.model)
        self.assertFalse(message.res_id)
        self.assertNotIn(lead.sudo().name, message.body)
        notifications = self.env["mail.notification"].search(
            [("mail_message_id", "=", message.id)]
        )
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications.notification_type, "inbox")
        self.assertEqual(self.env["mail.mail"].search_count([]), before)

    def test_erasure_clears_both_comparison_receipts_without_rebuilding(self):
        binding = self._whatsapp()
        lead = self._run(binding)
        projection = self._meta()
        self.assertEqual(self._project(projection), lead)
        self.connection = self.env["contact.center.provider.connection"].create(
            {
                "name": "Synthetic comparison erasure provider",
                "account_id": self.account.id,
                "adapter_key": "test.marketing.cc.crm",
                "external_ref": "synthetic-comparison-erasure",
                "state": "connected",
            }
        )
        with trap_jobs():
            _source, bridge = MarketingContactCenterCrmFixture._source_and_projection(
                self, binding, "synthetic-comparison-erasure", reconcile=False
            )
        point = bridge.marketing_touchpoint_id
        with self.assertRaises(AccessError):
            point._erase_private_values(token=True, now=fields.Datetime.now())
        point._erase_private_values(
            token=ATTRIBUTION_ERASURE_TOKEN, now=fields.Datetime.now()
        )
        projection.submission_id.touchpoint_id._erase_private_values(
            token=ATTRIBUTION_ERASURE_TOKEN, now=fields.Datetime.now()
        )
        for row, prefix in ((binding, "crm_comparison_"), (projection, "comparison_")):
            self.assertFalse(row[prefix + "exact"])
            self.assertFalse(row[prefix + "variant"])
            self.assertTrue(row[prefix + "erased_at"])
        self._run(binding)
        self._project(projection)
        self.assertFalse(binding.crm_comparison_exact)
        self.assertFalse(projection.comparison_exact)

    def test_intake_monitor_ignores_legacy_and_effective_manual_association(self):
        service = self.env["marketing.crm.service"]
        lead = self._project(self._meta())
        binding = self._whatsapp("551198765432")
        self.assertFalse(self._run(binding))
        binding._crm_intake_write(
            {
                "crm_intake_admitted_at": self.company.crm_cross_source_enabled_at
                - datetime.timedelta(seconds=1)
            }
        )
        self.assertEqual(
            service._crm_cross_source_intake_counts(self.company)["count"], 0
        )
        binding._crm_intake_write({"crm_intake_admitted_at": self.at})
        self.assertEqual(
            service._crm_cross_source_intake_counts(self.company)["count"], 1
        )
        with trap_jobs():
            self.env["contact.center.crm.conversation.link"].with_user(
                self.agent
            )._link(
                binding.channel_id.with_user(self.agent),
                lead.with_user(self.agent),
                writer="manual",
            )
        self.assertEqual(
            service._crm_cross_source_intake_counts(self.company)["count"], 0
        )
        self.assertEqual(binding.crm_intake_state, "review")
        with trap_jobs():
            self.env["contact.center.ui.api"].with_user(
                self.agent
            ).resolve_crm_intake_review(
                binding.channel_id.id, binding.crm_intake_revision, lead.id, True
            )
        self.assertEqual(binding.crm_intake_state, "resolved")
        self.assertEqual(
            service._crm_cross_source_intake_counts(self.company)["count"], 0
        )

    def test_technical_outage_is_retryable_with_one_inbox_signal(self):
        projection = self._meta()
        before = self.env["crm.lead"].search_count([])
        mail_before = self.env["mail.mail"].search_count([])
        target = type(self.env["marketing.crm.service"])
        with patch.object(
            target, "_crm_cross_source_check_gate", side_effect=CrmDedupUnavailable()
        ):
            self._project(projection)
        self.assertEqual(
            (projection.state, projection.technical_hold_reason, projection.attempts),
            ("pending", "bridge_missing", 0),
        )
        self.assertEqual(self.env["crm.lead"].search_count([]), before)
        message = projection.signal_message_id
        notifications = (
            self.env["mail.notification"]
            .sudo()
            .search([("mail_message_id", "=", message.id)])
        )
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications.notification_type, "inbox")
        self.assertEqual(self.env["mail.mail"].search_count([]), mail_before)
        with trap_jobs():
            projection._enqueue()
        self.assertTrue(self._project(projection))
        self.assertEqual(projection.state, "done")
        self.assertEqual(projection.signal_message_id, message)

    def test_policy_and_receipts_cannot_be_forged(self):
        with self.assertRaises(AccessError):
            self.company.with_user(self.agent).write(
                {"crm_cross_source_dedup_enabled": False}
            )
        with self.assertRaises(AccessError):
            self.company.write({"crm_cross_source_revision": 900})
        binding = self._whatsapp()
        with self.assertRaises(AccessError):
            binding.write({"crm_comparison_exact": "a" * 64})
        with self.assertRaises(AccessError):
            self.env["contact.center.crm.review.decision"].sudo().create({})

    def _commercial_review(self, phone="5511998765432"):
        binding = self._whatsapp(phone)
        lead = self._run(binding)
        lead.write({"active": False})
        projection = self._meta("+" + phone)
        self._project(projection)
        self.assertEqual(projection.state, "review")
        return binding, lead, projection

    def _review_queue(self):
        model = self.env["marketing.center.meta.crm.review.queue"].with_user(self.agent)
        action = model.action_open()
        return model.browse(action["res_id"])

    def test_manager_without_marketing_resolves_native_queue_with_no_vault_access(self):
        _binding, lead, projection = self._commercial_review()
        self.assertFalse(
            self.env["marketing.center.meta.lead.submission"]
            .with_user(self.agent)
            .check_access_rights("read", raise_exception=False)
        )
        queue = self._review_queue()
        line = queue.line_ids.filtered(
            lambda row: row.projection_ref == projection.public_ref
        )
        self.assertTrue(line)
        self.assertIn(lead, line.candidate_ids)
        with trap_jobs():
            line.write({"lead_id": lead.id, "confirmed": True})
            line.action_link()
        self.assertEqual(projection.state, "done")
        self.assertEqual(projection.lead_id, lead)
        self.assertEqual(projection.decision_actor_ref, self.agent.id)
        self.assertTrue(projection.review_decision_ref)
        self.assertTrue(projection.assertion_id)
        with self.assertRaises(ValidationError):
            projection.with_user(self.agent)._resolve_review("new", confirmed=True)

    def test_queue_rejects_arbitrary_candidate_before_many2one_serialization(self):
        _binding, _lead, projection = self._commercial_review()
        line = self._review_queue().line_ids.filtered(
            lambda row: row.projection_ref == projection.public_ref
        )
        unrelated = self.env["crm.lead"].create(
            {
                "name": "Unrelated confidential business",
                "company_id": self.company.id,
                "phone": "+5511988887777",
                "user_id": False,
            }
        )
        with self.assertRaises(AccessError):
            line.write({"lead_id": unrelated.id})
        with self.assertRaises(AccessError):
            line.onchange({"lead_id": unrelated.id}, "lead_id", {})
        with self.assertRaises(AccessError):
            self.env["marketing.center.meta.crm.review.line"].with_user(
                self.agent
            ).create(
                {"queue_id": line.queue_id.id, "projection_ref": projection.public_ref}
            )

    def test_human_dismissal_is_terminal_and_never_creates_a_business(self):
        _binding, _lead, projection = self._commercial_review()
        count = self.env["crm.lead"].with_context(active_test=False).search_count([])
        with trap_jobs():
            projection.with_user(self.agent)._resolve_review("dismiss", confirmed=True)
            projection._enqueue(retry_terminal=True)
            projection._mark_skipped()
            self.route.write({"crm_lead_title_prefix": "Route edit after dismissal"})
        self._project(projection)
        self.assertEqual(projection.state, "dismissed")
        self.assertFalse(projection.lead_res_id)
        self.assertFalse(projection.assertion_id)
        self.assertEqual(
            self.env["crm.lead"].with_context(active_test=False).search_count([]), count
        )

    def test_flag_off_keeps_technical_hold_until_audited_entry_release(self):
        projection = self._meta()
        target = type(self.env["marketing.crm.service"])
        with patch.object(
            target, "_crm_cross_source_check_gate", side_effect=CrmDedupUnavailable()
        ):
            self._project(projection)
        self.company.write({"crm_cross_source_dedup_enabled": False})
        self.assertFalse(self._project(projection))
        self.assertEqual(
            (projection.state, projection.technical_hold_reason),
            ("pending", "bridge_error"),
        )
        with trap_jobs():
            projection._release_technical_hold()
        self.assertTrue(projection.review_decision_ref)
        self.assertEqual(projection.decision_actor_ref, self.env.uid)
        self.assertTrue(self._project(projection))
        self.assertEqual(projection.state, "done")

    def test_disabled_bridge_can_disable_policy_without_blindly_releasing_entries(self):
        projection = self._meta()
        target = type(self.env["marketing.crm.service"])
        with patch.object(
            target, "_crm_cross_source_check_gate", side_effect=CrmDedupUnavailable()
        ):
            self._project(projection)
        with patch.object(target, "_crm_cross_source_capable", return_value=False):
            self.company.write({"crm_cross_source_dedup_enabled": False})
        self.assertFalse(self.company.crm_cross_source_dedup_enabled)
        self.assertTrue(projection.technical_hold_reason)
        self.assertFalse(self._project(projection))

    def test_monitor_new_incident_same_day_and_flag_off_hold_remain_visible(self):
        projection = self._meta()
        service = self.env["marketing.crm.service"]
        target = type(service)
        with patch.object(
            target, "_crm_cross_source_check_gate", side_effect=CrmDedupUnavailable()
        ):
            self._project(projection)
        mail_before = self.env["mail.mail"].search_count([])
        service._crm_admission_monitor()
        first = dict(self.company.crm_cross_source_monitor_json["technical_hold"])
        self.assertTrue(first["active"])
        self.assertEqual(first["incident"], 1)
        messages = self.env["mail.message"].search_count([])
        service._crm_admission_monitor()
        self.assertEqual(self.env["mail.message"].search_count([]), messages)
        with trap_jobs():
            projection._enqueue()
        self._project(projection)
        service._crm_admission_monitor()
        self.assertFalse(
            self.company.crm_cross_source_monitor_json["technical_hold"]["active"]
        )
        next_projection = self._meta("+5511988887777")
        with patch.object(
            target, "_crm_cross_source_check_gate", side_effect=CrmDedupUnavailable()
        ):
            self._project(next_projection)
        self.company.write({"crm_cross_source_dedup_enabled": False})
        service._crm_admission_monitor()
        second = self.company.crm_cross_source_monitor_json["technical_hold"]
        self.assertTrue(second["active"])
        self.assertEqual(second["incident"], 2)
        self.assertNotEqual(first["alert_key"], second["alert_key"])
        self.assertEqual(self.env["mail.mail"].search_count([]), mail_before)

    def test_erased_pending_projection_does_not_rebuild_comparison_receipt(self):
        projection = self._meta()
        point = projection.submission_id.touchpoint_id
        point._erase_private_values(
            token=ATTRIBUTION_ERASURE_TOKEN, now=fields.Datetime.now()
        )
        self.assertTrue(projection.comparison_erased_at)
        self.assertFalse(self._project(projection))
        self.assertEqual(projection.state, "review")
        self.assertFalse(projection.comparison_exact)
        self.assertFalse(projection.comparison_variant)

    def test_technical_hold_then_business_failure_becomes_failed_without_hold(self):
        projection = self._meta()
        with patch.object(
            type(self.env["marketing.crm.service"]),
            "_crm_cross_source_check_gate",
            side_effect=CrmDedupUnavailable(),
        ):
            self._project(projection)
        self.assertTrue(projection.technical_hold_reason)
        with trap_jobs():
            projection._enqueue()
        with patch.object(
            type(self.env["marketing.center.meta.crm.service"]),
            "_project",
            side_effect=ValidationError("Synthetic commercial rejection"),
        ):
            self._project(projection)
        self.env.flush_all()
        self.assertEqual(projection.state, "failed")
        self.assertFalse(projection.technical_hold_reason)
        self.assertFalse(projection.technical_hold_since)
        self.assertFalse(projection.next_technical_retry_at)

    def test_recovered_hold_on_disabled_route_can_skip(self):
        projection = self._meta()
        with patch.object(
            type(self.env["marketing.crm.service"]),
            "_crm_cross_source_check_gate",
            side_effect=CrmDedupUnavailable(),
        ):
            self._project(projection)
        with trap_jobs():
            self.route.write({"crm_auto_create_lead": False})
            projection._enqueue()
        self._project(projection)
        self.assertEqual(projection.state, "skipped")
        self.assertFalse(projection.technical_hold_reason)

    def test_review_queue_owner_and_company_rules_prevent_direct_pii_reads(self):
        _binding, _lead, projection = self._commercial_review()
        queue = self._review_queue()
        line = queue.line_ids.filtered(
            lambda row: row.projection_ref == projection.public_ref
        )
        other_manager = self._user(
            "Synthetic second manager", self.env.ref("sales_team.group_sale_manager")
        )
        other_company = self.env["res.company"].create(
            {"name": "Synthetic other queue company"}
        )
        foreign = self._user(
            "Synthetic foreign manager", self.env.ref("sales_team.group_sale_manager")
        )
        foreign.write(
            {
                "company_ids": [Command.set(other_company.ids)],
                "company_id": other_company.id,
            }
        )
        for actor in (other_manager, foreign):
            with self.subTest(actor=actor.id):
                ctx = {"allowed_company_ids": actor.company_ids.ids}
                self.assertFalse(
                    queue.with_user(actor)
                    .with_context(**ctx)
                    .search([("id", "=", queue.id)])
                )
                self.assertFalse(
                    line.with_user(actor)
                    .with_context(**ctx)
                    .search([("id", "=", line.id)])
                )
                with self.assertRaises(AccessError):
                    line.with_user(actor).with_context(**ctx).read(
                        ["name", "phone", "email"]
                    )
                with self.assertRaises(AccessError):
                    queue.with_user(actor).with_context(**ctx).read(["line_ids"])

    def test_disabled_route_cannot_turn_human_link_into_replayable_skip(self):
        _binding, lead, projection = self._commercial_review()
        with trap_jobs():
            self.route.write({"crm_auto_create_lead": False})
        with self.assertRaises(ValidationError):
            projection.with_user(self.agent)._resolve_review("link", lead.id, True)
        self.assertEqual(projection.state, "review")

    def test_intake_monitor_clears_after_dismiss_and_common_link(self):
        service = self.env["marketing.crm.service"]
        for dismiss, phone in ((True, "5511988887777"), (False, "5511977776666")):
            with self.subTest(dismiss=dismiss):
                lead = self.env["crm.lead"].create(
                    {
                        "name": "Synthetic monitor target",
                        "user_id": False,
                        "company_id": self.company.id,
                        "phone": "+" + phone,
                    }
                )
                self.env["crm.lead"].create(
                    {
                        "name": "Synthetic monitor other target",
                        "user_id": False,
                        "company_id": self.company.id,
                        "phone": "+" + phone,
                    }
                )
                binding = self._whatsapp(phone)
                self._run(binding)
                self.assertEqual(
                    service._crm_cross_source_intake_counts(self.company)["count"], 1
                )
                api = self.env["contact.center.ui.api"].with_user(self.agent)
                with trap_jobs():
                    if dismiss:
                        api.resolve_crm_intake_review(
                            binding.channel_id.id,
                            binding.crm_intake_revision,
                            False,
                            False,
                        )
                    else:
                        api.link_crm_opportunity(binding.channel_id.id, lead.id)
                self.assertEqual(
                    service._crm_cross_source_intake_counts(self.company)["count"], 0
                )

    def test_monitor_falls_back_to_policy_admin_after_reviewer_archived(self):
        admin = self._user(
            "Synthetic admission administrator",
            self.env.ref("base.group_system")
            | self.env.ref("marketing_center_base.group_marketing_center_admin")
            | self.env.ref("contact_center_base.group_contact_center_admin"),
        )
        reviewer = self._user(
            "Synthetic independent reviewer",
            self.env.ref("sales_team.group_sale_manager"),
        )
        self.company.write({"crm_cross_source_reviewer_id": reviewer.id})
        projection = self._meta()
        with patch.object(
            type(self.env["marketing.crm.service"]),
            "_crm_cross_source_check_gate",
            side_effect=CrmDedupUnavailable(),
        ):
            self._project(projection)
        reviewer.write({"active": False})
        before = self.env["mail.mail"].search_count([])
        self.env["marketing.crm.service"]._crm_admission_monitor()
        self.assertTrue(
            self.company.crm_cross_source_monitor_json["reviewer_unavailable"]
        )
        self.assertTrue(
            self.company.crm_cross_source_monitor_json["technical_hold"]["alert_key"]
        )
        notification = self.env["mail.notification"].search(
            [("res_partner_id", "=", admin.partner_id.id)]
        )
        self.assertTrue(notification)
        self.assertTrue(all(row.notification_type == "inbox" for row in notification))
        self.assertEqual(self.env["mail.mail"].search_count([]), before)

    def test_upstream_uninstall_is_blocked_before_removing_enabled_bridge(self):
        crm = self.env["ir.module.module"].search([("name", "=", "crm")])
        with self.assertRaises(ValidationError):
            crm.button_uninstall()
        self.assertEqual(crm.state, "installed")

    def test_hold_cron_keeps_each_projection_company_in_job_context(self):
        projection = self._meta()
        projection._internal_write(
            {
                "state": "pending",
                "technical_hold_reason": "bridge_missing",
                "technical_hold_since": self.at,
                "next_technical_retry_at": self.at,
            }
        )
        observed = []

        def enqueue(row, retry_terminal=False):
            observed.append(
                (row.id, row.env.company.id, row.env.context["allowed_company_ids"])
            )

        with patch.object(type(projection), "_enqueue", enqueue), patch.object(
            fields.Datetime, "now", return_value=self.at
        ):
            projection._cron_resume_technical_holds()
        self.assertIn(
            (projection.id, projection.company_id.id, projection.company_id.ids),
            observed,
        )

    def test_closed_coentry_receipt_outside_window_allows_distinct_new_demand(self):
        binding = self._whatsapp()
        previous = self._run(binding)
        with trap_jobs():
            previous.write({"active": False})
        self.at += datetime.timedelta(
            hours=self.company.crm_cross_source_window_hours, seconds=1
        )
        with patch.object(fields.Datetime, "now", return_value=self.at):
            projection = self._meta()
            lead = self._project(projection)
        self.assertTrue(lead)
        self.assertNotEqual(lead, previous)
        self.assertEqual(projection.state, "done")

    def test_history_cannot_hide_open_exact_or_variant_candidates(self):
        for number, incoming in [
            ("+5511997776666", "+5511997776666"),
            ("+551197776666", "+5511997776666"),
        ]:
            with self.subTest(phone=number):
                historical = self.env["crm.lead"].create(
                    [
                        {
                            "name": "Old history %s" % i,
                            "company_id": self.company.id,
                            "phone": number,
                            "active": False,
                        }
                        for i in range(5)
                    ]
                )
                lead = self.env["crm.lead"].create(
                    {
                        "name": "Current demand",
                        "company_id": self.company.id,
                        "phone": number,
                    }
                )
                projection = self._meta(phone=incoming)
                before = self.env["crm.lead"].search_count([])
                self.assertFalse(self._project(projection))
                self.assertEqual(projection.state, "review")
                self.assertIn(lead, projection.review_candidate_ids)
                self.assertEqual(self.env["crm.lead"].search_count([]), before)
                # Independent demand pair, without leaving the first subtest's
                # receipt in the second one's occurrence window.
                self.at += datetime.timedelta(hours=25)
                (historical | lead).write({"phone": False})

    def test_legacy_meta_first_then_whatsapp_keeps_review_without_authority(self):
        lead = self.env["crm.lead"].create(
            {
                "name": "Existing open business",
                "company_id": self.company.id,
                "phone": "+5511998765432",
            }
        )
        projection = self._meta()
        self.assertFalse(self._project(projection))
        self.assertEqual(projection.state, "review")
        binding = self._whatsapp()
        self.assertFalse(self._run(binding))
        self.assertEqual(binding.crm_intake_state, "review")
        self.assertFalse(projection.assertion_id)
        self.assertFalse(lead.source_id or lead.campaign_id)

    def test_healthy_monitor_repeat_does_not_write_company_or_incident(self):
        service = self.env["marketing.crm.service"]
        with trap_jobs():
            service._crm_admission_monitor()
        monitor = self.env["marketing.crm.admission.monitor"]
        with patch.object(
            type(self.company),
            "write",
            side_effect=AssertionError("company cache invalidation"),
        ), patch.object(
            type(monitor),
            "write",
            side_effect=AssertionError("unchanged incident write"),
        ):
            service._crm_admission_monitor()

    def test_unexpected_adapter_failure_becomes_visible_technical_hold(self):
        projection = self._meta()
        service = self.env["marketing.crm.service"]
        with patch.object(
            type(service),
            "_crm_cross_source_admit_meta",
            side_effect=TypeError("synthetic adapter failure"),
        ):
            self.assertFalse(self._project(projection))
        self.assertEqual(projection.state, "pending")
        self.assertEqual(projection.technical_hold_reason, "bridge_error")
        self.assertEqual(projection.attempts, 0)
        self.assertTrue(projection.signal_message_id)
        self.assertFalse(projection.assertion_id)

    def test_missing_provider_occurrence_is_business_review_without_hmac(self):
        with trap_jobs():
            submission = self._new_submission(
                fields={"phone_number": ("+5511998765432",)}
            )
            self._authenticate(submission, provider_created_at=False)
        projection = self._projection_for(submission)
        self.assertFalse(self._project(projection))
        self.assertEqual(projection.state, "review")
        self.assertEqual(projection.review_reason, "business_scope_review")
        self.assertFalse(projection.comparison_exact or projection.assertion_id)

import datetime
import hashlib
import json
import uuid
from unittest.mock import patch

from psycopg2 import OperationalError

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.marketing_center_base.services import MarketingTouchpointDTO
from odoo.addons.marketing_center_meta.services.tokens import (
    MARKETING_META_LEAD_INTERNAL_TOKEN,
)
from odoo.addons.queue_job.job import Job
from odoo.addons.queue_job.tests.common import trap_jobs


class TestMarketingCenterMetaCrmProjection(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suffix = uuid.uuid4().hex
        numeric_suffix = "%013d" % (uuid.uuid4().int % 10**13)
        cls.company = cls.env.company
        cls.app = (
            cls.env["meta.api.app"]
            .sudo()
            .create(
                {
                    "name": "Meta CRM test App %s" % suffix,
                    "company_id": cls.company.id,
                    "external_app_id": "91%s" % numeric_suffix,
                    "graph_version": "v26.0",
                    "credential_backend": "environment",
                    "app_secret_ref": "ODOO_META_CRM_TEST_APP_SECRET",
                }
            )
        )
        cls.profile = cls.env["marketing.center.meta.profile"].create(
            {
                "name": "Meta CRM test reader %s" % suffix,
                "company_id": cls.company.id,
                "meta_app_id": cls.app.id,
                "credential_backend": "environment",
                "access_token_ref": "ODOO_META_CRM_TEST_READER_TOKEN",
                "reader_kind": "lead_reader",
            }
        )
        cls.endpoint = cls.env["meta.webhook.endpoint"].create(
            {
                "name": "Meta CRM test endpoint %s" % suffix,
                "app_id": cls.app.id,
                "credential_backend": "environment",
                "verify_token_ref": "ODOO_META_CRM_TEST_VERIFY_TOKEN",
            }
        )
        cls.page = cls.env["meta.webhook.page"].create(
            {
                "name": "Meta CRM test Page %s" % suffix,
                "endpoint_id": cls.endpoint.id,
                "external_page_id": "92%s" % numeric_suffix,
                "credential_backend": "environment",
                "access_token_ref": "ODOO_META_CRM_TEST_PAGE_TOKEN",
            }
        )
        cls.salesperson = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Meta CRM owner %s" % suffix,
                    "login": "meta-crm-owner-%s" % suffix,
                    "company_id": cls.company.id,
                    "company_ids": [Command.set(cls.company.ids)],
                    "groups_id": [Command.set(cls.env.ref("base.group_user").ids)],
                }
            )
        )
        cls.team = cls.env["crm.team"].create(
            {
                "name": "Meta CRM team %s" % suffix,
                "company_id": cls.company.id,
                "member_ids": [Command.set(cls.salesperson.ids)],
            }
        )
        cls.tag = cls.env["crm.tag"].create({"name": "Meta Lead Ads %s" % suffix})
        cls.source = cls.env["marketing.center.source"].create(
            {
                "name": "Meta CRM source %s" % suffix,
                "company_id": cls.company.id,
                "service": "meta.ads",
                "external_account_ref": "act_95%s" % numeric_suffix,
                "currency_id": cls.company.currency_id.id,
                "timezone": "UTC",
                "state": "active",
            }
        )
        cls.route = cls.env["marketing.center.meta.lead.route"].create(
            {
                "name": "Meta CRM route %s" % suffix,
                "webhook_page_id": cls.page.id,
                "lead_profile_id": cls.profile.id,
                "source_id": cls.source.id,
                "external_form_id": "93%s" % numeric_suffix,
                "crm_team_id": cls.team.id,
                "crm_user_id": cls.salesperson.id,
                "crm_tag_ids": [Command.set(cls.tag.ids)],
                "crm_lead_title_prefix": "Meta test",
            }
        )
        cls.service = cls.env["marketing.center.meta.crm.service"]

    def _new_submission(self, *, fields=None, authenticate=False):
        unique = uuid.uuid4().int % 10**14
        submission = (
            self.env["marketing.center.meta.lead.submission"]
            .sudo()
            .with_context(
                marketing_meta_lead_internal_token=MARKETING_META_LEAD_INTERNAL_TOKEN
            )
            .create(
                {
                    "company_id": self.company.id,
                    "route_id": self.route.id,
                    "meta_app_id": self.app.id,
                    "origin": "reconciliation",
                    "leadgen_id": "94%014d" % unique,
                    "page_id_hint": self.page.external_page_id,
                    "form_id_hint": self.route.external_form_id,
                    "hint_sha256": hashlib.sha256(
                        ("meta-crm-hint:%s" % unique).encode()
                    ).hexdigest(),
                }
            )
        )
        field_values = fields or {
            "full_name": ("Private Person",),
            "email": ("person@example.com",),
            "phone_number": ("+55 19 99999-0000",),
            "company_name": ("Private Company",),
            "custom_sensitive_answer": ("must remain private",),
            "description": ("must never reach CRM",),
        }
        field_model = (
            self.env["marketing.center.meta.lead.field"]
            .sudo()
            .with_context(
                marketing_meta_lead_internal_token=MARKETING_META_LEAD_INTERNAL_TOKEN
            )
        )
        field_model.create(
            [
                {
                    "submission_id": submission.id,
                    "sequence": sequence,
                    "field_name": name,
                    "values_json": list(values),
                    "value_count": len(values),
                    "values_sha256": hashlib.sha256(
                        json.dumps(
                            list(values), separators=(",", ":"), sort_keys=True
                        ).encode()
                    ).hexdigest(),
                }
                for sequence, (name, values) in enumerate(field_values.items())
            ]
        )
        if authenticate:
            self._authenticate(submission, len(field_values))
        return submission

    def _touchpoint(self, submission):
        result = self.env["marketing.attribution.service"]._ingest_touchpoint(
            self.company,
            MarketingTouchpointDTO(
                source_system="meta.lead_ads",
                source_scope_ref="meta-crm-suite:%s" % self.route.public_ref,
                source_occurrence_ref="leadgen:%s" % submission.leadgen_id,
                source_evidence_ref="meta.lead.submission:%s" % submission.public_ref,
                source_schema_version="meta.lead_ads.v1",
                occurred_at=datetime.datetime(2026, 9, 1, 12, 0),
                platform="meta",
                channel="lead_ads",
                network="facebook",
                touchpoint_type="lead_ad",
                evidence_level="provider_asserted",
            ),
        )
        return self.env["marketing.attribution.touchpoint"].browse(result.touchpoint_id)

    def _authenticate(self, submission, field_count=None, provider_created_at=None):
        touchpoint = self._touchpoint(submission)
        submission.with_context(
            marketing_meta_lead_internal_token=MARKETING_META_LEAD_INTERNAL_TOKEN
        ).write(
            {
                "state": "ingested",
                "processed_at": datetime.datetime(2026, 9, 1, 12, 1),
                "provider_created_at": (
                    datetime.datetime(2026, 9, 1, 12, 0)
                    if provider_created_at is None else provider_created_at
                ),
                "provider_payload_sha256": "a" * 64,
                "field_count": (
                    len(submission.field_ids) if field_count is None else field_count
                ),
                "touchpoint_id": touchpoint.id,
            }
        )
        return touchpoint

    def _enable_route(self):
        # These legacy scenarios explicitly opt into their historical fixture.
        with trap_jobs():
            self.route.write({
                "crm_auto_create_lead": True,
                "crm_history_policy": "all",
            })
        with trap_jobs() as jobs:
            self.company._job_marketing_meta_crm_backfill()
        return jobs

    def _projection_for(self, submission):
        return self.env["marketing.center.meta.crm.projection"].search(
            [("submission_id", "=", submission.id)]
        )

    def _run_projection_job(self, projection):
        return projection.with_context(
            job_uuid=projection.queue_job_uuid
        )._job_project_to_crm()

    def _running_projection_job(self, projection, *, completed_tries):
        job = Job(projection._job_project_to_crm, max_retries=8)
        job.store()
        job.retry = completed_tries
        job.set_started()
        job.store()
        projection._internal_write({"queue_job_uuid": str(job.uuid)})
        return job

    def test_opt_in_backfills_only_authenticated_ingested_submissions(self):
        pending = self._new_submission()
        authenticated = self._new_submission(authenticate=True)
        self.assertFalse(self._projection_for(pending))
        self.assertFalse(self._projection_for(authenticated))
        with self.assertRaises(ValidationError):
            self.service._ensure_projection(pending)

        jobs = self._enable_route()
        jobs.assert_jobs_count(1)
        self.assertFalse(self._projection_for(pending))
        projection = self._projection_for(authenticated)
        self.assertEqual(len(projection), 1)
        self.assertEqual(projection.state, "pending")

    def test_company_bootstrap_resumes_by_cursor_without_duplicate_evidence(self):
        first = self._new_submission(authenticate=True)
        second = self._new_submission(authenticate=True)
        self.assertLess(first.id, second.id)
        self.assertFalse(self._projection_for(first))
        self.assertFalse(self._projection_for(second))
        with trap_jobs():
            self.route.write({
                "crm_auto_create_lead": True,
                "crm_history_policy": "all",
            })

        with trap_jobs() as jobs:
            first_page = self.company._job_marketing_meta_crm_backfill(limit=1)
            jobs.assert_jobs_count(2)
            jobs.assert_enqueued_job(
                self.company._job_marketing_meta_crm_backfill,
                args=(first.id, 1),
            )
        self.assertFalse(first_page["done"])
        self.assertEqual(first_page["last_submission_id"], first.id)
        self.assertTrue(self._projection_for(first))
        self.assertFalse(self._projection_for(second))

        with trap_jobs() as jobs:
            second_page = self.company._job_marketing_meta_crm_backfill(
                after_submission_id=first.id,
                limit=1,
            )
            jobs.assert_jobs_count(2)
            jobs.assert_enqueued_job(
                self.company._job_marketing_meta_crm_backfill,
                args=(second.id, 1),
            )
        self.assertFalse(second_page["done"])
        self.assertTrue(self._projection_for(second))

        with trap_jobs() as jobs:
            final_page = self.company._job_marketing_meta_crm_backfill(
                after_submission_id=second.id,
                limit=1,
            )
            jobs.assert_jobs_count(0)
        self.assertTrue(final_page["done"])
        self.assertEqual(final_page["last_submission_id"], second.id)
        self.assertEqual(
            self.env["marketing.center.meta.crm.projection"].search_count(
                [("submission_id", "in", (first.id, second.id))]
            ),
            2,
        )

    def test_ingested_label_without_authenticated_evidence_is_rejected(self):
        submission = self._new_submission()
        wrong_touchpoint = self.env["marketing.attribution.service"]._ingest_touchpoint(
            self.company,
            MarketingTouchpointDTO(
                source_system="test.manual",
                source_scope_ref="meta-crm-suite",
                source_occurrence_ref="manual:%s" % submission.public_ref,
                occurred_at=datetime.datetime(2026, 9, 1, 12, 0),
                platform="meta",
                channel="lead_ads",
                touchpoint_type="lead_ad",
                evidence_level="provider_asserted",
            ),
        )
        submission.with_context(
            marketing_meta_lead_internal_token=MARKETING_META_LEAD_INTERNAL_TOKEN
        ).write(
            {
                "state": "ingested",
                "provider_payload_sha256": "b" * 64,
                "touchpoint_id": wrong_touchpoint.touchpoint_id,
            }
        )
        with self.assertRaises(ValidationError):
            self.service._ensure_projection(submission)

    def test_projection_is_idempotent_allowlisted_owned_and_partnerless(self):
        self._enable_route().assert_jobs_count(0)
        partner_count = self.env["res.partner"].search_count([])
        with trap_jobs() as jobs:
            submission = self._new_submission(authenticate=True)
        jobs.assert_jobs_count(1)
        projection = self._projection_for(submission).ensure_one()

        self.assertTrue(self._run_projection_job(projection))
        lead = projection.lead_id
        assertion = projection.assertion_id
        self.assertEqual(projection.state, "done")
        self.assertEqual(lead.team_id, self.team)
        self.assertEqual(lead.user_id, self.salesperson)
        self.assertEqual(lead.tag_ids, self.tag)
        self.assertFalse(lead.partner_id)
        self.assertEqual(lead.contact_name, "Private Person")
        self.assertEqual(lead.email_from, "person@example.com")
        self.assertEqual(lead.phone, "+55 19 99999-0000")
        self.assertEqual(lead.partner_name, "Private Company")
        self.assertFalse(lead.description)
        self.assertNotIn("must remain private", str(lead.read()[0]))
        self.assertNotIn("must never reach CRM", str(lead.read()[0]))
        self.assertEqual(self.env["res.partner"].search_count([]), partner_count)

        lead_count = self.env["crm.lead"].search_count([("id", "=", lead.id)])
        self.assertTrue(self._run_projection_job(projection))
        replayed_projection = self.service._ensure_projection(submission)
        replayed_assertion = self.env["marketing.crm.service"]._link_touchpoint_lead(
            submission.touchpoint_id,
            lead,
            assertion.source_ref,
            authority_key="meta.lead_submission",
            authority_ref=submission.public_ref,
            assertion_ref="meta-lead-submission:%s" % submission.public_ref,
        )
        self.assertEqual(replayed_projection, projection)
        self.assertEqual(replayed_assertion, assertion)
        self.assertEqual(
            self.env["crm.lead"].search_count([("id", "=", lead.id)]), lead_count
        )
        self.assertEqual(assertion.authority_key, "meta.lead_submission")
        self.assertEqual(assertion.authority_ref, submission.public_ref)
        self.assertEqual(assertion.touchpoint_id, submission.touchpoint_id)
        with self.assertRaises(AccessError):
            assertion.sudo().unlink()

        effective = self.env["marketing.attribution.crm.effective.link"].search(
            [
                ("touchpoint_id", "=", submission.touchpoint_id.id),
                ("lead_id", "=", lead.id),
            ]
        )
        self.assertEqual(len(effective), 1)
        self.assertEqual(effective.assertion_count, 1)

    def test_projected_lead_unlink_keeps_meta_evidence_without_live_action(self):
        self._enable_route()
        with trap_jobs():
            submission = self._new_submission(authenticate=True)
        projection = self._projection_for(submission).ensure_one()
        self.assertTrue(self._run_projection_job(projection))
        lead = projection.lead_id
        lead_id = lead.id
        lead_name = lead.name
        assertion = projection.assertion_id

        self.assertTrue(lead.unlink())
        projection.invalidate_recordset(["lead_id"])
        assertion.invalidate_recordset(["lead_id"])
        submission.invalidate_recordset(["crm_lead_id"])
        self.assertEqual(projection.state, "done")
        self.assertFalse(projection.lead_id)
        self.assertFalse(assertion.lead_id)
        self.assertFalse(submission.crm_lead_id)
        self.assertEqual(projection.lead_model, "crm.lead")
        self.assertEqual(projection.lead_res_id, lead_id)
        self.assertEqual(projection.lead_display_ref, lead_name)
        self.assertEqual(assertion.lead_res_id, lead_id)
        self.assertFalse(projection.action_open_lead())

    def test_assertion_failure_rolls_back_lead_and_never_creates_partner(self):
        self._enable_route()
        with trap_jobs():
            submission = self._new_submission(authenticate=True)
        projection = self._projection_for(submission).ensure_one()
        lead_count = self.env["crm.lead"].search_count([])
        partner_count = self.env["res.partner"].search_count([])
        target = (
            "odoo.addons.marketing_center_crm.models.service."
            "MarketingCrmService._link_touchpoint_lead"
        )
        with patch(target, side_effect=ValidationError("assertion rejected")):
            self.assertFalse(self._run_projection_job(projection))
        self.assertEqual(projection.state, "failed")
        self.assertFalse(projection.lead_id)
        self.assertFalse(projection.assertion_id)
        self.assertEqual(self.env["crm.lead"].search_count([]), lead_count)
        self.assertEqual(self.env["res.partner"].search_count([]), partner_count)

    def test_orphan_projection_job_cannot_create_a_crm_lead(self):
        self._enable_route()
        with trap_jobs():
            submission = self._new_submission(authenticate=True)
        projection = self._projection_for(submission).ensure_one()
        lead_count = self.env["crm.lead"].search_count([])
        target = (
            "odoo.addons.marketing_center_meta_crm.models.service."
            "MarketingCenterMetaCrmService._project"
        )
        with patch(target) as project:
            self.assertFalse(
                projection.with_context(
                    job_uuid=str(uuid.uuid4())
                )._job_project_to_crm()
            )
        project.assert_not_called()
        self.assertEqual(projection.state, "pending")
        self.assertEqual(self.env["crm.lead"].search_count([]), lead_count)

    def test_terminal_database_retry_becomes_operator_recoverable(self):
        self._enable_route()
        with trap_jobs():
            submission = self._new_submission(authenticate=True)
        projection = self._projection_for(submission).ensure_one()
        job = self._running_projection_job(projection, completed_tries=7)
        target = (
            "odoo.addons.marketing_center_meta_crm.models.service."
            "MarketingCenterMetaCrmService._project"
        )
        with patch(target, side_effect=OperationalError("synthetic lock conflict")):
            self.assertFalse(job.perform())

        projection.invalidate_recordset(
            [
                "state",
                "attempts",
                "queue_job_uuid",
                "last_error_class",
                "last_error_message",
            ]
        )
        self.assertEqual(projection.state, "failed")
        self.assertEqual(projection.attempts, 8)
        self.assertFalse(projection.queue_job_uuid)
        self.assertEqual(projection.last_error_class, "ConcurrentDatabaseRetryLimit")
        self.assertIn("retry policy", projection.last_error_message)

    def test_route_configuration_rejects_foreign_company_ownership(self):
        other_company = self.env["res.company"].create(
            {"name": "Meta CRM isolated %s" % uuid.uuid4().hex}
        )
        other_team = (
            self.env["crm.team"]
            .with_company(other_company)
            .create(
                {
                    "name": "Foreign Meta CRM team",
                    "company_id": other_company.id,
                }
            )
        )
        context = {"allowed_company_ids": [self.company.id, other_company.id]}
        with self.assertRaises(UserError):
            self.route.with_context(**context).write({"crm_team_id": other_team.id})

        other_user = (
            self.env["res.users"]
            .with_context(
                no_reset_password=True,
                allowed_company_ids=[self.company.id, other_company.id],
            )
            .create(
                {
                    "name": "Foreign Meta CRM owner",
                    "login": "foreign-meta-owner-%s" % uuid.uuid4().hex,
                    "company_id": other_company.id,
                    "company_ids": [Command.set(other_company.ids)],
                    "groups_id": [Command.set(self.env.ref("base.group_user").ids)],
                }
            )
        )
        with self.assertRaises(ValidationError):
            self.route.with_context(**context).write({"crm_user_id": other_user.id})

    def test_only_marketing_admin_can_enable_projection(self):
        viewer = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Meta CRM viewer",
                    "login": "meta-crm-viewer-%s" % uuid.uuid4().hex,
                    "company_id": self.company.id,
                    "company_ids": [Command.set(self.company.ids)],
                    "groups_id": [
                        Command.set(
                            self.env.ref(
                                "marketing_center_base.group_marketing_center_viewer"
                            ).ids
                        )
                    ],
                }
            )
        )
        with self.assertRaises(AccessError):
            self.route.with_user(viewer).write({"crm_auto_create_lead": True})

    def test_first_activation_excludes_history_and_late_old_submissions(self):
        cutoff = datetime.datetime(2026, 9, 15, 12, 0)
        old = self._new_submission(authenticate=True)
        self.assertEqual(self.route.crm_history_policy, "since")
        self.assertFalse(self.route.crm_create_from)
        with patch.object(fields.Datetime, "now", return_value=cutoff), trap_jobs() as jobs:
            self.route.write({"crm_auto_create_lead": True})
        self.assertEqual(self.route.crm_create_from, cutoff)
        jobs.assert_jobs_count(1)
        jobs.assert_enqueued_job(
            self.company._job_marketing_meta_crm_backfill, args=(0, 200),
            kwargs={"route_ids": (self.route.id,)},
        )
        self.assertFalse(self._projection_for(old))
        with trap_jobs() as jobs:
            page = self.company._job_marketing_meta_crm_backfill(limit=1)
        self.assertEqual(page["last_submission_id"], old.id)
        self.assertFalse(self._projection_for(old))
        # Arrival time is not the cutoff: a later sync can fetch an old Meta lead.
        with trap_jobs() as jobs:
            late_old = self._new_submission(authenticate=True)
        jobs.assert_jobs_count(0)
        self.assertFalse(self._projection_for(late_old))
        current = self._new_submission()
        with trap_jobs() as jobs:
            self._authenticate(current, provider_created_at=cutoff)
        jobs.assert_jobs_count(1)
        projection = self._projection_for(current).ensure_one()
        self.assertTrue(self._run_projection_job(projection))
        self.assertTrue(projection.lead_id)

    def test_cutoff_is_preserved_on_reactivation_and_manual_reconcile(self):
        cutoff = datetime.datetime(2026, 9, 15, 12, 0)
        old = self._new_submission(authenticate=True)
        with trap_jobs():
            self.route.write({
                "crm_auto_create_lead": True,
                "crm_create_from": cutoff,
            })
            self.route.write({"crm_auto_create_lead": False})
            self.route.write({"crm_auto_create_lead": True})
        self.assertEqual(self.route.crm_create_from, cutoff)
        with trap_jobs() as jobs:
            self.route.action_enqueue_crm_backfill()
        jobs.assert_jobs_count(1)
        with trap_jobs():
            self.company._job_marketing_meta_crm_backfill()
        self.assertFalse(self._projection_for(old))
        # Retroactive inclusion is an explicit operator choice.
        with trap_jobs():
            self.route.write({"crm_create_from": datetime.datetime(2026, 9, 1, 0, 0)})
            self.company._job_marketing_meta_crm_backfill()
        self.assertTrue(self._projection_for(old))

    def test_missing_provider_date_is_excluded_by_cutoff(self):
        with trap_jobs():
            self.route.write({"crm_auto_create_lead": True})
            submission = self._new_submission()
            self._authenticate(submission, provider_created_at=False)
        self.assertFalse(self._projection_for(submission))
        with trap_jobs():
            self.route.write({"crm_history_policy": "all"})
            self.company._job_marketing_meta_crm_backfill()
        self.assertTrue(self._projection_for(submission))

    def test_policy_change_fences_queued_job_and_preserves_done_lead(self):
        self._enable_route()
        with trap_jobs():
            done_submission = self._new_submission(authenticate=True)
            pending_submission = self._new_submission(authenticate=True)
        done = self._projection_for(done_submission)
        pending = self._projection_for(pending_submission)
        pending_uuid = pending.queue_job_uuid
        self.assertTrue(self._run_projection_job(done))
        lead = done.lead_id
        original_type = lead.type
        lead_count = self.env["crm.lead"].search_count([])
        with trap_jobs():
            self.route.write({
                "crm_history_policy": "since",
                "crm_create_from": datetime.datetime(2026, 9, 15, 12, 0),
                "crm_lead_type": "opportunity" if original_type == "lead" else "lead",
            })
        self.assertEqual(pending.state, "skipped")
        self.assertFalse(pending.with_context(job_uuid=pending_uuid)._job_project_to_crm())
        with trap_jobs() as jobs:
            pending.action_retry()
        jobs.assert_jobs_count(0)
        self.assertTrue(self.service._project(pending))
        self.assertFalse(pending.lead_id)
        self.assertEqual(done.state, "done")
        self.assertEqual(done.lead_id, lead)
        self.assertEqual(lead.type, original_type)
        self.assertEqual(self.env["crm.lead"].search_count([]), lead_count)

    def test_native_record_type_follows_global_setting_without_owner(self):
        self.route.write({"crm_team_id": False, "crm_user_id": False})
        self.assertEqual(self.route.crm_lead_type, "native")
        self._enable_route()
        root = self.env.ref("base.user_root")
        group = self.env.ref("crm.group_use_lead")
        for enabled, expected in ((True, "lead"), (False, "opportunity")):
            root.write({"groups_id": [Command.link(group.id) if enabled else Command.unlink(group.id)]})
            with trap_jobs():
                submission = self._new_submission(authenticate=True)
            projection = self._projection_for(submission)
            self.assertTrue(self._run_projection_job(projection))
            self.assertEqual(projection.lead_id.type, expected)
            self.assertFalse(projection.lead_id.team_id)
            self.assertFalse(projection.lead_id.user_id)
            self.assertEqual(projection.lead_id.company_id, self.company)

    def test_native_record_type_follows_configured_team(self):
        self._enable_route()
        for enabled, expected in ((True, "lead"), (False, "opportunity")):
            self.team.write({"use_leads": enabled})
            with trap_jobs():
                submission = self._new_submission(authenticate=True)
            projection = self._projection_for(submission)
            self.assertTrue(self._run_projection_job(projection))
            self.assertEqual(projection.lead_id.type, expected)
            self.assertEqual(projection.lead_id.team_id, self.team)

    def test_explicit_record_type_overrides_team_setting(self):
        self._enable_route()
        for requested in ("lead", "opportunity"):
            self.team.write({"use_leads": requested != "lead"})
            with trap_jobs():
                self.route.write({"crm_lead_type": requested})
                submission = self._new_submission(authenticate=True)
            projection = self._projection_for(submission)
            self.assertTrue(self._run_projection_job(projection))
            self.assertEqual(projection.lead_id.type, requested)

    def test_enabled_route_create_initializes_cutoff_without_onchange(self):
        cutoff = datetime.datetime(2026, 9, 15, 12, 0)
        values = {
            "name": "New community CRM route",
            "webhook_page_id": self.page.id,
            "lead_profile_id": self.profile.id,
            "source_id": self.source.id,
            "external_form_id": "98%014d" % (uuid.uuid4().int % 10**14),
            "crm_auto_create_lead": True,
        }
        with patch.object(fields.Datetime, "now", return_value=cutoff), trap_jobs():
            route = self.env["marketing.center.meta.lead.route"].create(values)
        self.assertEqual(route.crm_create_from, cutoff)
        self.assertEqual(route.crm_history_policy, "since")
        self.assertEqual(route.crm_lead_type, "native")
        self.assertFalse(route.crm_team_id)
        self.assertFalse(route.crm_user_id)

    def test_batch_activation_preserves_each_existing_cutoff(self):
        cutoff = datetime.datetime(2026, 9, 15, 12, 0)
        earlier = datetime.datetime(2026, 9, 14, 12, 0)
        second = self.env["marketing.center.meta.lead.route"].create({
            "name": "Second community CRM route",
            "webhook_page_id": self.page.id,
            "lead_profile_id": self.profile.id,
            "source_id": self.source.id,
            "external_form_id": "98%014d" % (uuid.uuid4().int % 10**14),
            "crm_create_from": earlier,
        })
        with patch.object(fields.Datetime, "now", return_value=cutoff), trap_jobs():
            (self.route | second).write({"crm_auto_create_lead": True})
        self.assertEqual(self.route.crm_create_from, cutoff)
        self.assertEqual(second.crm_create_from, earlier)

    def test_context_default_activation_also_initializes_cutoff(self):
        cutoff = datetime.datetime(2026, 9, 15, 12, 0)
        with patch.object(fields.Datetime, "now", return_value=cutoff), trap_jobs():
            route = self.env["marketing.center.meta.lead.route"].with_context(
                default_crm_auto_create_lead=True,
            ).create({
                "name": "Context-enabled CRM route",
                "webhook_page_id": self.page.id,
                "lead_profile_id": self.profile.id,
                "source_id": self.source.id,
                "external_form_id": "98%014d" % (uuid.uuid4().int % 10**14),
            })
        self.assertTrue(route.crm_auto_create_lead)
        self.assertEqual(route.crm_create_from, cutoff)

    def test_team_and_salesperson_can_be_selected_independently(self):
        self._enable_route()
        for team_id, user_id in ((False, self.salesperson.id), (self.team.id, False)):
            with trap_jobs():
                self.route.write({"crm_team_id": team_id, "crm_user_id": user_id})
                submission = self._new_submission(authenticate=True)
            projection = self._projection_for(submission)
            self.assertTrue(self._run_projection_job(projection))
            self.assertEqual(projection.lead_id.team_id.id, team_id)
            self.assertEqual(projection.lead_id.user_id.id, user_id)
            self.assertEqual(projection.lead_id.company_id, self.company)

    def test_reconciliation_does_not_retry_other_routes(self):
        first_route = self.route
        with trap_jobs():
            first_route.write({"crm_auto_create_lead": True, "crm_history_policy": "all"})
            first_submission = self._new_submission(authenticate=True)
        second_route = self.env["marketing.center.meta.lead.route"].create({
            "name": "Independent CRM route",
            "webhook_page_id": self.page.id,
            "lead_profile_id": self.profile.id,
            "source_id": self.source.id,
            "external_form_id": "98%014d" % (uuid.uuid4().int % 10**14),
            "crm_auto_create_lead": True,
            "crm_history_policy": "all",
        })
        self.route = second_route
        try:
            with trap_jobs():
                second_submission = self._new_submission(authenticate=True)
        finally:
            self.route = first_route
        first = self._projection_for(first_submission)
        second = self._projection_for(second_submission)
        (first | second)._internal_write({"state": "failed", "queue_job_uuid": False})
        with trap_jobs() as jobs:
            first_route.action_enqueue_crm_backfill()
        jobs.assert_jobs_count(1)
        jobs.assert_enqueued_job(
            self.company._job_marketing_meta_crm_backfill, args=(0, 200),
            kwargs={"route_ids": (first_route.id,)},
        )
        with trap_jobs() as jobs:
            self.company._job_marketing_meta_crm_backfill(route_ids=(first_route.id,))
        jobs.assert_jobs_count(1)
        self.assertEqual(first.state, "pending")
        self.assertEqual(second.state, "failed")
        self.assertFalse(second.queue_job_uuid)

    def test_persisted_scoped_job_roundtrip_and_pagination(self):
        with trap_jobs():
            self.route.write({"crm_auto_create_lead": True, "crm_history_policy": "all"})
        first = self._new_submission()
        second = self._new_submission()
        with trap_jobs():
            self._authenticate(first)
            self._authenticate(second)
        # Simulate a failed attempt so that only this reconciliation revives it.
        self._projection_for(first)._internal_write({"state": "failed", "queue_job_uuid": False})
        original = Job(
            self.company._job_marketing_meta_crm_backfill,
            args=(0, 1), kwargs={"route_ids": (self.route.id,)},
        )
        original.store()
        loaded = Job.load(self.env, original.uuid)
        self.assertEqual(list(loaded.kwargs["route_ids"]), [self.route.id])
        with trap_jobs() as jobs:
            result = loaded.perform()
        self.assertFalse(result["done"])
        self.assertEqual(result["last_submission_id"], first.id)
        self.assertEqual(self._projection_for(first).state, "pending")
        jobs.assert_enqueued_job(
            self.company._job_marketing_meta_crm_backfill, args=(first.id, 1),
            kwargs={"route_ids": (self.route.id,)},
        )

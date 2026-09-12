import datetime
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests.common import SavepointCase

from odoo.addons.meta_api_base.services.errors import (
    MetaApiPausedError,
    MetaApiRateLimitError,
)
from odoo.addons.queue_job.job import Job
from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.adapter import MetaReadValidation
from ..services.tokens import MARKETING_META_PROFILE_RUNTIME_TOKEN
from .common import create_meta_profile


class TestMetaCredentialHealth(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app, cls.profile = create_meta_profile(
            cls.env,
            name="Credential health laboratory",
            external_app_id="919283747",
            app_secret_ref="ODOO_META_HEALTH_SECRET",
            access_token_ref="ODOO_META_HEALTH_TOKEN",
        )
        cls.service = cls.env["marketing.center.meta.service"]
        cls.now = datetime.datetime(2026, 9, 12, 12)
        cls.validation = MetaReadValidation(
            token_type="system_user",
            expires_at=0,
            data_access_expires_at=0,
            scopes=("ads_read",),
            capabilities={"read_entities": True},
        )

    def _runtime(self, profile=None):
        return (profile or self.profile).with_context(
            marketing_meta_profile_runtime_token=MARKETING_META_PROFILE_RUNTIME_TOKEN
        )

    def _job(self):
        job = Job(
            self.profile._job_validate_read_profile,
            kwargs={
                "expected_profile_revision": self.profile.profile_revision,
                "expected_app_revision": self.app.revision,
            },
            max_retries=8,
            identity_key=self.profile._validation_job_identity(),
        )
        job.store()
        return job

    def test_scheduler_is_bounded_staggered_and_idempotent(self):
        with trap_jobs() as trap:
            result = self.profile._cron_enqueue_credential_health(limit=1, now=self.now)
            replay = self.profile._cron_enqueue_credential_health(limit=1, now=self.now)
        self.assertEqual(result, {"queued": 1, "skipped": 0})
        self.assertEqual(replay, {"queued": 0, "skipped": 0})
        trap.assert_jobs_count(1)
        job = trap.enqueued_jobs[0]
        self.assertEqual(job.identity_key, self.profile._validation_job_identity())
        self.assertGreaterEqual(job.eta, self.now)
        self.assertLess(job.eta, self.now + datetime.timedelta(minutes=5))
        self.assertEqual(job.max_retries, 8)

    def test_scheduler_deduplicates_started_manual_validation(self):
        job = self._job()
        job.set_started()
        job.store()
        with trap_jobs() as trap:
            result = self.profile._cron_enqueue_credential_health(now=self.now)
        trap.assert_jobs_count(0)
        self.assertEqual(result["skipped"], 1)

    def test_success_clears_diagnostic_and_schedules_next_check(self):
        self._runtime().write(
            {"last_error_trace_id": "OldTrace", "health_check_failures": 4}
        )
        with patch("odoo.fields.Datetime.now", return_value=self.now):
            self.service._mark_profile_success(self.profile, self.validation)
        self.assertFalse(self.profile.last_error_trace_id)
        self.assertEqual(self.profile.health_check_failures, 0)
        self.assertGreaterEqual(
            self.profile.next_health_check_at, self.now + datetime.timedelta(days=1)
        )
        self.assertEqual(self.profile.credential_alert, "none")

    def test_error_diagnostics_and_bounded_backoff_persist_without_secret_body(self):
        error = MetaApiPausedError(
            "Meta Graph authorization is unavailable",
            provider_code=190,
            provider_subcode=463,
            provider_trace_id="Trace_123",
            http_status=401,
            usage_call_count_percent=75,
            retry_after_seconds=7200,
        )
        with patch("odoo.fields.Datetime.now", return_value=self.now):
            self.service._mark_profile_failure(self.profile, error, paused=True)
        self.assertEqual(self.profile.last_error_provider_code, 190)
        self.assertEqual(self.profile.last_error_provider_subcode, 463)
        self.assertEqual(self.profile.last_error_trace_id, "Trace_123")
        self.assertEqual(self.profile.last_error_http_status, 401)
        self.assertEqual(self.profile.last_usage_call_percent, 75)
        self.assertEqual(
            self.profile.next_health_check_at, self.now + datetime.timedelta(hours=2)
        )
        self.assertEqual(self.profile.credential_alert, "review")
        with trap_jobs() as trap:
            self.profile._cron_enqueue_credential_health(
                now=self.now + datetime.timedelta(hours=1)
            )
        trap.assert_jobs_count(0)

    def test_terminal_retry_records_metadata_and_stale_revision_does_not(self):
        job = self._job()
        job.retry = 7
        job.set_started()
        job.store()
        error = MetaApiRateLimitError(
            "Meta Graph rate limit is active",
            retry_after_seconds=900,
            provider_code=4,
            provider_trace_id="RateTrace",
            usage_call_count_percent=100,
        )
        with patch(
            "odoo.addons.marketing_center_meta.models.meta_service.MetaMarketingReadAdapter"
        ) as adapter:
            adapter.return_value.validate.side_effect = error
            self.service.with_context(job_uuid=job.uuid)._validate_profile(
                self.profile.with_context(job_uuid=job.uuid),
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.app.revision,
            )
        self.assertEqual(self.profile.last_error_trace_id, "RateTrace")
        self.assertEqual(self.profile.last_usage_call_percent, 100)
        revision = self.profile.profile_revision
        self.profile.access_token_ref = "ODOO_META_HEALTH_ROTATED"
        self.assertFalse(self.profile.last_error_trace_id)
        self.assertFalse(self.profile.next_health_check_at)
        result = self.service._validate_profile(
            self.profile,
            expected_profile_revision=revision,
            expected_app_revision=self.app.revision,
        )
        self.assertEqual(result, {"stale": True})
        self.assertFalse(self.profile.last_error_trace_id)

    def test_expiry_alert_uses_earliest_credential_expiry_and_near_check(self):
        self._runtime().write(
            {
                "health_state": "healthy",
                "verified_at": self.now,
                "token_expires_at": self.now + datetime.timedelta(days=20),
                "data_access_expires_at": self.now + datetime.timedelta(days=2),
            }
        )
        with patch("odoo.fields.Datetime.now", return_value=self.now):
            self.profile._compute_credential_alert()
        self.assertEqual(self.profile.credential_alert, "expiring")
        self._runtime().write(
            {"data_access_expires_at": self.now - datetime.timedelta(seconds=1)}
        )
        with patch("odoo.fields.Datetime.now", return_value=self.now):
            self.profile._compute_credential_alert()
        self.assertEqual(self.profile.credential_alert, "expired")

    def test_scheduler_respects_company_and_reader_scope(self):
        other = self.env["res.company"].create({"name": "Other health company"})
        _other_app, other_profile = create_meta_profile(
            self.env["res.company"].with_company(other).env,
            name="Other health",
            external_app_id="919283748",
            app_secret_ref="ODOO_META_OTHER_SECRET",
            access_token_ref="ODOO_META_OTHER_TOKEN",
        )
        _lead_app, lead = create_meta_profile(
            self.env,
            name="Lead health",
            external_app_id="919283749",
            app_secret_ref="ODOO_META_LEAD_SECRET",
            access_token_ref="ODOO_META_LEAD_TOKEN",
            reader_kind="lead_reader",
        )
        with trap_jobs() as trap:
            self.profile.with_context(
                allowed_company_ids=self.env.company.ids
            )._cron_enqueue_credential_health(now=self.now)
        trap.assert_jobs_count(1)
        self.assertFalse(other_profile.next_health_check_at)
        self.assertFalse(lead.next_health_check_at)
        with self.assertRaises(AccessError):
            self.profile.write({"last_error_provider_code": 190})

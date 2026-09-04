import datetime
import uuid
from unittest.mock import patch

from odoo.tests.common import SavepointCase

from odoo.addons.google_api_base.services.errors import GoogleApiRateLimitError
from odoo.addons.marketing_center_base.services.performance_dto import (
    MarketingPerformanceDTO,
    PerformancePageDTO,
)

from ..services.performance import (
    GOOGLE_PERFORMANCE_GRAINS,
    normalize_performance_window,
)
from .common import create_google_profile, project_google_source

_ADAPTER_PATH = (
    "odoo.addons.marketing_center_google.models.performance_sync."
    "GoogleMarketingReadAdapter"
)

_RESOURCE = {
    "account": "customers/1234567890",
    "campaign": "customers/1234567890/campaigns/10",
    "ad_group": "customers/1234567890/adGroups/20",
    "ad": "customers/1234567890/adGroupAds/20~30",
    "keyword": "customers/1234567890/adGroupCriteria/20~40",
}


class TestGooglePerformanceSync(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.identity, cls.profile = create_google_profile(
            cls.env, name="Google performance laboratory"
        )
        _customer, cls.source, cls.connection = project_google_source(
            cls.env,
            cls.profile,
            timezone="America/Sao_Paulo",
            login_customer_id="2222222222",
        )
        cls.service = cls.env["marketing.center.google.performance.service"]
        cls.date_from = datetime.date(2026, 8, 30)
        cls.date_to = datetime.date(2026, 8, 31)

    def _plan(self, grain, *, trigger_kind="manual"):
        run = self.service._plan_performance(
            self.source,
            self.connection,
            grain=grain,
            date_from=self.date_from,
            date_to=self.date_to,
            trigger_kind=trigger_kind,
            trigger_ref="test:%s:%s" % (grain, uuid.uuid4()),
        )
        sequence = self.service._restart_cursor(run)
        self.service._enqueue_performance_page(run, sequence)
        return run, sequence

    def _metric(self, run, *, report_date=None):
        report_date = report_date or self.date_to
        _date_from, _date_to, start, end = normalize_performance_window(
            report_date, report_date, self.source.timezone
        )
        return MarketingPerformanceDTO(
            grain=run.grain,
            entity_external_ref=_RESOURCE[run.grain],
            report_date=report_date,
            period_start_utc=start,
            period_end_utc=end,
            report_timezone=self.source.timezone,
            currency=self.source.currency_id.name,
            observed_at=datetime.datetime(2026, 9, 1, 12, 0),
            impressions=100,
            clicks=5,
            cost_micros=1234567,
            reporting_context_hash=run.reporting_context_hash,
            source_schema_version="google.ads.performance.daily.v25.1",
        )

    def _page(self, run, items=(), **values):
        return PerformancePageDTO(
            items=items,
            reporting_context_hash=run.reporting_context_hash,
            **values,
        )

    def _execute(self, run, sequence, pages):
        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_performance_pages.return_value = pages
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_sync_google_performance_page(sequence)
        return result, adapter_class

    def test_cursor_mismatch_reschedules_exact_authoritative_page(self):
        run, expected_sequence = self._plan("account")
        authoritative_sequence = expected_sequence + 2
        old_job_uuid = run.queue_job_uuid
        sync_service = self.env["marketing.center.sync.service"]
        cursor = sync_service._locked_cursor(run)
        sync_service._write_cursor(
            cursor,
            {"cursor_sequence": authoritative_sequence},
        )

        with patch(_ADAPTER_PATH) as adapter_class:
            result = run.with_context(
                job_uuid=old_job_uuid
            )._job_sync_google_performance_page(expected_sequence)

        run.invalidate_recordset(["queue_job_uuid", "state"])
        replacement = (
            self.env["queue.job"]
            .sudo()
            .search([("uuid", "=", run.queue_job_uuid)], limit=1)
        )
        self.assertEqual(
            result,
            {
                "cursor_changed": True,
                "rescheduled": True,
                "cursor_sequence": authoritative_sequence,
            },
        )
        self.assertEqual(run.state, "queued")
        self.assertNotEqual(run.queue_job_uuid, old_job_uuid)
        self.assertTrue(
            replacement.identity_key.endswith(":%s" % authoritative_sequence)
        )
        adapter_class.assert_not_called()

    def test_all_daily_grains_project_through_provider_neutral_dto(self):
        for grain in GOOGLE_PERFORMANCE_GRAINS:
            with self.subTest(grain=grain):
                run, sequence = self._plan(grain)
                result, _adapter = self._execute(
                    run, sequence, (self._page(run, (self._metric(run),)),)
                )
                self.assertEqual(result["state"], "succeeded")
        metrics = self.env["marketing.center.metric.daily"].search(
            [("source_id", "=", self.source.id), ("report_date", "=", self.date_to)]
        )
        self.assertEqual(set(metrics.mapped("grain")), set(GOOGLE_PERFORMANCE_GRAINS))
        self.assertEqual(len(metrics), len(GOOGLE_PERFORMANCE_GRAINS))

    def test_performance_pagination_preserves_window_and_opaque_cursor(self):
        run, sequence = self._plan("campaign")
        first, _adapter = self._execute(
            run,
            sequence,
            (self._page(run, next_cursor="opaque-page-2", has_more=True),),
        )
        self.assertTrue(first["has_more"])
        second, adapter_class = self._execute(
            run,
            sequence + 1,
            (self._page(run),),
        )
        self.assertEqual(second["state"], "succeeded")
        call = adapter_class.return_value.fetch_performance_pages.call_args
        self.assertEqual(
            adapter_class.call_args.kwargs["login_customer_id"],
            "2222222222",
        )
        self.assertEqual(call.args, (self.source.external_account_id, "campaign"))
        self.assertEqual(call.kwargs["page_token"], "opaque-page-2")
        self.assertEqual(call.kwargs["date_from"], self.date_from)
        self.assertEqual(call.kwargs["date_to"], self.date_to)

    def test_profile_rotation_discards_metrics_after_provider_io(self):
        run, sequence = self._plan("account")
        page = self._page(run, (self._metric(run),))

        def rotate(*_args, **_kwargs):
            self.identity.write({"developer_token_ref": "GOOGLE_METRIC_ROTATED"})
            return (page,)

        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_performance_pages.side_effect = rotate
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_sync_google_performance_page(sequence)
        self.assertEqual(result, {"stale": True})
        self.assertEqual(run.state, "stale")
        self.assertFalse(
            self.env["marketing.center.metric.daily"].search(
                [("source_id", "=", self.source.id)]
            )
        )

    def test_performance_provider_io_precedes_execution_row_locks(self):
        run, sequence = self._plan("account")
        page = self._page(run, (self._metric(run),))
        service_class = type(self.service)
        original_lock = service_class._lock_execution_fences
        lock_calls = []

        def traced_lock(service, *args, **kwargs):
            lock_calls.append(True)
            return original_lock(service, *args, **kwargs)

        def provider_page(*_args, **_kwargs):
            self.assertFalse(lock_calls)
            return (page,)

        with patch.object(service_class, "_lock_execution_fences", traced_lock):
            with patch(_ADAPTER_PATH) as adapter_class:
                adapter_class.return_value.fetch_performance_pages.side_effect = (
                    provider_page
                )
                result = run.with_context(
                    job_uuid=run.queue_job_uuid
                )._job_sync_google_performance_page(sequence)
        self.assertEqual(result["state"], "succeeded")
        self.assertTrue(lock_calls)

    def test_performance_quota_uses_a_committable_eta_successor(self):
        run, sequence = self._plan("account")
        original_job_uuid = run.queue_job_uuid
        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_performance_pages.side_effect = (
                GoogleApiRateLimitError(
                    "synthetic metric quota",
                    retry_after_seconds=91,
                    provider_status="RESOURCE_EXHAUSTED",
                )
            )
            result = run.with_context(
                job_uuid=original_job_uuid
            )._job_sync_google_performance_page(sequence)
        run.invalidate_recordset(["queue_job_uuid", "state"])
        self.connection.invalidate_recordset(
            ["cooldown_until", "health_state", "last_health_error_class"]
        )
        self.assertEqual(
            result,
            {"rescheduled": True, "retry_after": 91, "quota_attempt": 1},
        )
        self.assertNotEqual(run.queue_job_uuid, original_job_uuid)
        self.assertEqual(self.service._cursor_snapshot(run)[0], sequence)
        self.assertEqual(self.connection.health_state, "degraded")
        self.assertTrue(self.connection.cooldown_until)

    def test_closed_window_scheduler_is_idempotent_for_all_grains(self):
        now = datetime.datetime(2026, 9, 1, 12, 0)
        first = self.service._cron_enqueue_google_performance(
            source_ids=[self.source.id], limit=1, now=now, lookback_days=2
        )
        second = self.service._cron_enqueue_google_performance(
            source_ids=[self.source.id], limit=1, now=now, lookback_days=2
        )
        expected = len(GOOGLE_PERFORMANCE_GRAINS)
        self.assertEqual(first, {"queued": expected, "skipped": 0, "failed": 0})
        self.assertEqual(second, {"queued": 0, "skipped": expected, "failed": 0})

import datetime
import uuid
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.marketing_center_base.services.performance_dto import (
    MarketingPerformanceDTO,
    PerformancePageDTO,
)
from odoo.addons.marketing_center_base.services.tokens import MARKETING_SYNC_WRITE_TOKEN
from odoo.addons.meta_api_base.services.errors import (
    MetaApiError,
    MetaApiPausedError,
    MetaApiTransientError,
)
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.adapter import MetaAdAccount
from ..services.insights import normalize_insights_window

_ADAPTER_PATH = (
    "odoo.addons.marketing_center_meta.models.insights_sync." "MetaMarketingReadAdapter"
)
_SERVICE_PATH = (
    "odoo.addons.marketing_center_meta.models.insights_sync."
    "MarketingCenterMetaInsightsService"
)


class TestMetaInsightsSync(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.profile = cls.env["marketing.center.meta.profile"].create(
            {
                "name": "Meta Insights laboratory",
                "company_id": cls.env.company.id,
                "external_app_id": "987654321",
                "credential_backend": "environment",
                "app_secret_ref": "ODOO_META_INSIGHTS_APP_SECRET",
                "access_token_ref": "ODOO_META_INSIGHTS_READER_TOKEN",
            }
        )
        cls.account_ref = "act_987654"
        cls.currency = cls.env.company.currency_id.name
        account = MetaAdAccount(
            external_ref=cls.account_ref,
            external_id="987654",
            name="Meta Insights Laboratory",
            currency=cls.currency,
            timezone="America/Sao_Paulo",
            account_status=1,
            disable_reason=0,
        )
        cls.capabilities = {
            "read_entities": True,
            "read_metrics": True,
            "receive_leads": False,
        }
        cls.source = cls.env["marketing.center.meta.service"]._upsert_source(
            cls.profile,
            cls.capabilities,
            account,
        )
        cls.connection = cls.env["marketing.center.connection"].search(
            [
                ("source_id", "=", cls.source.id),
                ("adapter_key", "=", "meta.graph"),
            ],
            limit=1,
        )
        cls.service = cls.env["marketing.center.meta.catalog.service"]
        cls.date_from = datetime.date(2026, 8, 24)
        cls.date_to = datetime.date(2026, 8, 30)
        cls.observed_at = datetime.datetime(2026, 8, 31, 12, 0)

    def _plan(
        self,
        grain="account",
        *,
        trigger_kind="manual",
        restart_from_start=False,
    ):
        run = self.service._plan_insights(
            self.source,
            self.connection,
            grain=grain,
            date_from=self.date_from,
            date_to=self.date_to,
            trigger_kind=trigger_kind,
            trigger_ref="test:%s:%s" % (grain, uuid.uuid4()),
        )
        sequence = (
            self.service._restart_insights_cursor(run)
            if restart_from_start
            else self.service._insights_cursor_snapshot(run)[0]
        )
        self.service._enqueue_insights_page(run, sequence)
        return run, sequence

    def _metric(
        self,
        run,
        *,
        report_date=None,
        entity_external_ref=None,
        impressions=10,
        clicks=2,
        cost_micros=1234567,
        observed_at=None,
    ):
        report_date = report_date or self.date_to
        _date_from, _date_to, period_start, period_end = normalize_insights_window(
            report_date,
            report_date,
            self.source.timezone,
        )
        return MarketingPerformanceDTO(
            grain=run.grain,
            entity_external_ref=(
                entity_external_ref
                or (
                    self.account_ref
                    if run.grain == "account"
                    else "%s/campaigns/456" % self.account_ref
                )
            ),
            report_date=report_date,
            period_start_utc=period_start,
            period_end_utc=period_end,
            report_timezone=self.source.timezone,
            currency=self.currency,
            observed_at=observed_at or self.observed_at,
            impressions=impressions,
            clicks=clicks,
            cost_micros=cost_micros,
            dimensions={},
            metric_origin="platform_reported",
            reporting_context_hash=run.reporting_context_hash,
            source_schema_version="meta.marketing.insights.daily.v1",
        )

    def _page(self, run, items=(), **values):
        return PerformancePageDTO(
            items=items,
            reporting_context_hash=run.reporting_context_hash,
            **values,
        )

    def _execute(self, run, sequence, provider_page):
        job_uuid = run.queue_job_uuid
        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_insights_page.return_value = provider_page
            result = run.with_context(job_uuid=job_uuid)._job_sync_meta_insights_page(
                sequence
            )
        return result, adapter_class

    def test_account_and_historical_campaign_are_projected_exactly(self):
        account_run, account_sequence = self._plan("account")
        account_metric = self._metric(
            account_run,
            impressions=3000000001,
            clicks=None,
            cost_micros=987654321012345,
        )
        result, _adapter = self._execute(
            account_run,
            account_sequence,
            self._page(account_run, (account_metric,)),
        )
        self.assertEqual(result["state"], "succeeded")
        current = self.env["marketing.center.metric.daily"].search(
            [
                ("source_id", "=", self.source.id),
                ("grain", "=", "account"),
                ("report_date", "=", self.date_to),
            ]
        )
        self.assertEqual(len(current), 1)
        self.assertEqual(current.impressions, 3000000001)
        self.assertFalse(current.has_clicks)
        self.assertEqual(current.clicks, 0)
        self.assertEqual(current.cost_micros, 987654321012345)
        self.assertEqual(current.current_revision_sequence, 1)
        self.assertEqual(current.last_sync_run_id, account_run)

        campaign_run, campaign_sequence = self._plan("campaign")
        campaign_ref = "%s/campaigns/999999" % self.account_ref
        campaign_metric = self._metric(
            campaign_run,
            entity_external_ref=campaign_ref,
        )
        self._execute(
            campaign_run,
            campaign_sequence,
            self._page(campaign_run, (campaign_metric,)),
        )
        historical = self.env["marketing.center.metric.daily"].search(
            [
                ("source_id", "=", self.source.id),
                ("entity_external_ref", "=", campaign_ref),
            ]
        )
        self.assertEqual(len(historical), 1)
        self.assertFalse(historical.entity_id)
        self.assertEqual(historical.grain, "campaign")

    def test_pagination_advances_cursor_and_replaces_successor_job(self):
        run, sequence = self._plan()
        first_job_uuid = run.queue_job_uuid
        first, _adapter = self._execute(
            run,
            sequence,
            self._page(run, next_cursor="opaque-page-2", has_more=True),
        )
        self.assertEqual(first["state"], "running")
        self.assertTrue(first["has_more"])
        self.assertNotEqual(run.queue_job_uuid, first_job_uuid)
        successor_uuid = run.queue_job_uuid
        self.assertEqual(
            self.service._insights_cursor_snapshot(run),
            (1, "opaque-page-2"),
        )

        terminal, adapter_class = self._execute(run, 1, self._page(run))
        self.assertEqual(terminal["state"], "succeeded")
        call = adapter_class.return_value.fetch_insights_page.call_args
        self.assertEqual(call.args, (self.account_ref, "account"))
        self.assertEqual(call.kwargs["after"], "opaque-page-2")
        self.assertEqual(call.kwargs["date_from"], self.date_from)
        self.assertEqual(call.kwargs["date_to"], self.date_to)
        self.assertEqual(run.queue_job_uuid, successor_uuid)
        self.assertEqual(run.page_count, 2)

    def test_empty_later_run_never_fabricates_zero_or_fresh_observation(self):
        first_run, first_sequence = self._plan()
        metric = self._metric(first_run, impressions=25, clicks=3, cost_micros=500000)
        self._execute(
            first_run,
            first_sequence,
            self._page(first_run, (metric,)),
        )
        current = self.env["marketing.center.metric.daily"].search(
            [("source_id", "=", self.source.id), ("grain", "=", "account")]
        )
        revision = current.current_revision_id

        empty_run, empty_sequence = self._plan("account", trigger_kind="scheduled")
        self._execute(empty_run, empty_sequence, self._page(empty_run))
        current.invalidate_recordset()
        self.assertEqual(empty_run.state, "succeeded")
        self.assertEqual(current.impressions, 25)
        self.assertEqual(current.current_revision_id, revision)
        self.assertEqual(current.current_revision_sequence, 1)
        self.assertEqual(current.last_sync_run_id, first_run)
        cursor = self.env["marketing.center.sync.cursor"].search(
            [
                ("source_id", "=", self.source.id),
                ("cursor_kind", "=", "metrics"),
                ("grain", "=", "account"),
            ],
            limit=1,
        )
        self.assertEqual(cursor.last_success_run_id, empty_run)

    def test_profile_rotation_during_io_discards_page(self):
        run, sequence = self._plan()
        page = self._page(run, (self._metric(run),))

        def rotate(*_args, **_kwargs):
            self.profile.write({"access_token_ref": "ODOO_META_INSIGHTS_ROTATED_TOKEN"})
            return page

        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_insights_page.side_effect = rotate
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_sync_meta_insights_page(sequence)
        self.assertEqual(result, {"stale": True})
        self.assertEqual(run.state, "stale")
        self.assertEqual(run.page_count, 0)
        self.assertFalse(
            self.env["marketing.center.metric.daily"].search(
                [("source_id", "=", self.source.id)]
            )
        )

    def test_orphan_job_never_crosses_provider_boundary(self):
        run, sequence = self._plan()
        with patch(_ADAPTER_PATH) as adapter_class:
            result = run.with_context(
                job_uuid=str(uuid.uuid4())
            )._job_sync_meta_insights_page(sequence)
        self.assertEqual(result, {"orphan": True})
        adapter_class.assert_not_called()
        self.assertEqual(run.state, "queued")

    def test_transient_retry_commits_terminal_failure_at_ceiling(self):
        run, sequence = self._plan()
        job = (
            self.env["queue.job"]
            .sudo()
            .search([("uuid", "=", run.queue_job_uuid)], limit=1)
        )
        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_insights_page.side_effect = (
                MetaApiTransientError("synthetic transient")
            )
            with self.assertRaises(RetryableJobError):
                run.with_context(
                    job_uuid=run.queue_job_uuid
                )._job_sync_meta_insights_page(sequence)
            self.assertEqual(run.state, "queued")
            job.sudo().write({"retry": 7})
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_sync_meta_insights_page(sequence)
        self.assertEqual(result, {"state": "failed"})
        self.assertEqual(run.state, "failed")
        self.assertEqual(run.error_class, "transient")
        self.assertEqual(run.page_count, 0)
        replacement, _sequence = self._plan(trigger_kind="retry")
        self.assertEqual(replacement.state, "queued")

    def test_authorization_pause_updates_health_without_projecting(self):
        run, sequence = self._plan()
        binding_revision = self.connection.binding_revision
        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_insights_page.side_effect = (
                MetaApiPausedError("synthetic authorization revoked")
            )
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_sync_meta_insights_page(sequence)
        self.assertEqual(result, {"stale": True, "profile_paused": True})
        self.assertEqual(run.state, "stale")
        self.assertEqual(run.page_count, 0)
        self.assertEqual(self.profile.health_state, "unhealthy")
        self.assertEqual(self.connection.state, "paused")
        self.assertEqual(self.connection.health_state, "unhealthy")
        self.assertGreater(self.connection.binding_revision, binding_revision)
        self.assertFalse(
            self.env["marketing.center.metric.daily"].search(
                [("source_id", "=", self.source.id)]
            )
        )

    def test_page_cap_stops_before_an_extra_provider_call(self):
        run, sequence = self._plan()
        self._execute(
            run,
            sequence,
            self._page(run, next_cursor="opaque-page-2", has_more=True),
        )
        with patch(
            "odoo.addons.marketing_center_meta.models.insights_sync."
            "_MAX_INSIGHTS_PAGES",
            1,
        ), patch(_ADAPTER_PATH) as adapter_class:
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_sync_meta_insights_page(1)
        self.assertEqual(result, {"state": "partial"})
        self.assertEqual(run.state, "partial")
        self.assertEqual(run.page_count, 1)
        adapter_class.assert_not_called()

    def test_permanent_failure_after_first_page_preserves_partial_coverage(self):
        run, sequence = self._plan()
        first_metric = self._metric(run)
        self._execute(
            run,
            sequence,
            self._page(
                run,
                (first_metric,),
                next_cursor="opaque-page-2",
                has_more=True,
            ),
        )
        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_insights_page.side_effect = MetaApiError(
                "synthetic permanent error"
            )
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_sync_meta_insights_page(1)
        self.assertEqual(result, {"state": "partial"})
        self.assertEqual(run.state, "partial")
        self.assertEqual(run.page_count, 1)
        self.assertEqual(run.received_count, 1)
        metric = self.env["marketing.center.metric.daily"].search(
            [("source_id", "=", self.source.id)], limit=1
        )
        self.assertEqual(metric.last_sync_run_id, run)
        self.assertEqual(metric.current_revision_sequence, 1)
        self.assertEqual(
            self.service._insights_cursor_snapshot(run),
            (1, "opaque-page-2"),
        )

    def test_unexpected_successor_failure_rolls_back_page_and_cursor(self):
        run, sequence = self._plan()
        job = (
            self.env["queue.job"]
            .sudo()
            .search([("uuid", "=", run.queue_job_uuid)], limit=1)
        )
        job.sudo().write({"retry": 7})
        page = self._page(
            run,
            (self._metric(run),),
            next_cursor="opaque-page-2",
            has_more=True,
        )
        with patch(_ADAPTER_PATH) as adapter_class, patch(
            "%s._enqueue_insights_page" % _SERVICE_PATH,
            side_effect=RuntimeError("access_token=synthetic-secret-must-not-persist"),
        ):
            adapter_class.return_value.fetch_insights_page.return_value = page
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_sync_meta_insights_page(sequence)
        self.assertEqual(result, {"state": "failed"})
        self.assertEqual(run.state, "failed")
        self.assertEqual(run.page_count, 0)
        self.assertNotIn("synthetic-secret", run.error_summary or "")
        self.assertFalse(
            self.env["marketing.center.metric.daily"].search(
                [("source_id", "=", self.source.id)]
            )
        )
        self.assertFalse(
            self.env["marketing.center.sync.cursor"].search(
                [
                    ("source_id", "=", self.source.id),
                    ("cursor_kind", "=", "metrics"),
                ]
            )
        )

    def test_invalid_cursor_closes_scope_without_provider_io(self):
        run, sequence = self._plan()
        self._execute(
            run,
            sequence,
            self._page(run, next_cursor="valid-next", has_more=True),
        )
        cursor = self.env["marketing.center.sync.cursor"].search(
            [
                ("source_id", "=", self.source.id),
                ("cursor_kind", "=", "metrics"),
                ("grain", "=", "account"),
            ],
            limit=1,
        )
        cursor.with_context(
            marketing_sync_write_token=MARKETING_SYNC_WRITE_TOKEN
        ).write({"cursor_value": "bad\nvalue"})
        with patch(_ADAPTER_PATH) as adapter_class:
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_sync_meta_insights_page(1)
        self.assertEqual(result, {"state": "partial"})
        self.assertEqual(run.state, "partial")
        adapter_class.assert_not_called()
        replacement, replacement_sequence = self._plan(
            trigger_kind="retry",
            restart_from_start=True,
        )
        self.assertEqual(replacement.state, "queued")
        terminal, adapter_class = self._execute(
            replacement,
            replacement_sequence,
            self._page(replacement),
        )
        self.assertEqual(terminal["state"], "succeeded")
        self.assertEqual(
            adapter_class.return_value.fetch_insights_page.call_args.kwargs["after"],
            "",
        )

    def test_metrics_capability_is_enforced_inside_planning_service(self):
        account = MetaAdAccount(
            external_ref="act_987655",
            external_id="987655",
            name="Meta Entities Only",
            currency=self.currency,
            timezone="America/Sao_Paulo",
            account_status=1,
            disable_reason=0,
        )
        source = self.env["marketing.center.meta.service"]._upsert_source(
            self.profile,
            {
                "read_entities": True,
                "read_metrics": False,
                "receive_leads": False,
            },
            account,
        )
        connection = self.env["marketing.center.connection"].search(
            [("source_id", "=", source.id)], limit=1
        )
        with self.assertRaises(ValidationError):
            self.service._plan_insights(
                source,
                connection,
                grain="account",
                date_from=self.date_from,
                date_to=self.date_to,
                trigger_kind="manual",
                trigger_ref="test:%s" % uuid.uuid4(),
            )

import datetime
import uuid

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from ..services.catalog_dto import ExternalEntityDTO
from ..services.performance_dto import MarketingPerformanceDTO, PerformancePageDTO
from ..services.tokens import MARKETING_PERFORMANCE_WRITE_TOKEN


class TestMarketingPerformanceService(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = cls.env["marketing.center.source"].create(
            {
                "name": "Performance source",
                "company_id": cls.env.company.id,
                "service": "meta.ads",
                "external_account_ref": "act_performance_lab",
                "currency_id": cls.env.company.currency_id.id,
                "timezone": "UTC",
                "state": "active",
            }
        )
        cls.connection = cls.env["marketing.center.connection"].create(
            {
                "name": "Performance reader",
                "source_id": cls.source.id,
                "adapter_key": "meta.graph",
                "purpose": "reader",
                "profile_public_ref": "profile-performance-reader",
                "profile_revision": 1,
                "state": "ready",
            }
        )
        cls.performance_service = cls.env["marketing.center.performance.service"]
        cls.sync_service = cls.env["marketing.center.sync.service"]

    def _dto(self, **overrides):
        values = {
            "grain": "campaign",
            "entity_external_ref": "act_performance_lab/campaigns/100",
            "report_date": datetime.date(2026, 8, 30),
            "period_start_utc": datetime.datetime(2026, 8, 30, 0, 0),
            "period_end_utc": datetime.datetime(2026, 8, 31, 0, 0),
            "report_timezone": "UTC",
            "currency": self.source.currency_id.name,
            "observed_at": datetime.datetime(2026, 8, 31, 10, 0),
            "impressions": 5_000_000_000,
            "clicks": 100,
            "cost_micros": 123_000_000,
        }
        values.update(overrides)
        return MarketingPerformanceDTO(**values)

    def _plan(self, **overrides):
        grain = overrides.get("grain", "campaign")
        report_timezone = overrides.get("report_timezone", "UTC")
        values = {
            "sync_kind": "metrics",
            "grain": grain,
            "scope_ref": "account:act_performance_lab:campaign",
            "trigger_kind": "manual",
            "trigger_ref": str(uuid.uuid4()),
            "reporting_context": {
                "contract_version": "test.performance.daily.v1",
                "grain": grain,
                "currency": self.source.currency_id.name,
                "report_timezone": report_timezone,
                "level": grain,
                "time_increment": 1,
            },
            "window_start": datetime.datetime(2026, 8, 30, 0, 0),
            "window_end": datetime.datetime(2026, 8, 31, 0, 0),
            "report_timezone": "UTC",
        }
        values.update(overrides)
        return self.sync_service._plan_run(
            self.env.company,
            self.source,
            self.connection,
            **values,
        )

    def _run_dto(self, run, **overrides):
        values = {"reporting_context_hash": run.reporting_context_hash}
        values.update(overrides)
        return self._dto(**values)

    def test_exact_replay_and_a_b_a_revision_history(self):
        first = self.performance_service._upsert_metric(
            self.env.company, self.source, self._dto()
        )
        replay = self.performance_service._upsert_metric(
            self.env.company,
            self.source,
            self._dto(observed_at=datetime.datetime(2026, 8, 31, 11, 0)),
        )
        state_b = self.performance_service._upsert_metric(
            self.env.company, self.source, self._dto(clicks=101)
        )
        second_a = self.performance_service._upsert_metric(
            self.env.company, self.source, self._dto()
        )
        metric = self.env["marketing.center.metric.daily"].browse(first.metric_id)
        self.assertEqual(replay.disposition, "duplicate")
        self.assertEqual(state_b.revision_sequence, 2)
        self.assertEqual(second_a.revision_sequence, 3)
        self.assertEqual(
            metric.revision_ids.sorted("revision_sequence").mapped("revision_sequence"),
            [1, 2, 3],
        )
        self.assertEqual(metric.impressions, 5_000_000_000)
        self.assertFalse(metric.dimensions_json)
        self.assertEqual(metric.dimension_hash, self._dto().dimension_hash)

    def test_optional_entity_is_linked_on_later_replay(self):
        result = self.performance_service._upsert_metric(
            self.env.company, self.source, self._dto()
        )
        metric = self.env["marketing.center.metric.daily"].browse(result.metric_id)
        self.assertFalse(metric.entity_id)
        entity_result = self.env["marketing.center.catalog.service"]._upsert_entity(
            self.env.company,
            self.source,
            ExternalEntityDTO(
                entity_type="campaign",
                external_ref=self._dto().entity_external_ref,
                name="Campaign 100",
                observed_at=datetime.datetime(2026, 8, 31, 12, 0),
            ),
        )
        replay = self.performance_service._upsert_metric(
            self.env.company, self.source, self._dto()
        )
        self.assertEqual(replay.disposition, "duplicate")
        self.assertEqual(metric.entity_id.id, entity_result.entity_id)
        self.assertEqual(metric.current_revision_sequence, 1)

    def test_wrong_cursor_cas_does_not_apply_metrics(self):
        run = self._plan()
        page = PerformancePageDTO(
            items=(self._run_dto(run),),
            reporting_context_hash=run.reporting_context_hash,
        )
        with self.assertRaises(ValidationError):
            self.sync_service._apply_performance_page(
                run, page, expected_cursor_sequence=1
            )
        self.assertFalse(
            self.env["marketing.center.metric.daily"].search(
                [("source_id", "=", self.source.id)]
            )
        )
        self.assertEqual(run.state, "planned")

    def test_sync_replay_updates_last_run_and_empty_run_is_not_tombstone(self):
        first_run = self._plan()
        first_page = PerformancePageDTO(
            items=(self._run_dto(first_run),),
            reporting_context_hash=first_run.reporting_context_hash,
            authoritative_complete=True,
        )
        self.sync_service._apply_performance_page(
            first_run, first_page, expected_cursor_sequence=0
        )
        metric = self.env["marketing.center.metric.daily"].search(
            [("source_id", "=", self.source.id)], limit=1
        )
        self.assertEqual(first_run.state, "succeeded")
        self.assertEqual(metric.current_revision_sequence, 1)

        replay_run = self._plan()
        replay_page = PerformancePageDTO(
            items=(self._run_dto(replay_run),),
            reporting_context_hash=replay_run.reporting_context_hash,
            authoritative_complete=True,
        )
        self.sync_service._apply_performance_page(
            replay_run, replay_page, expected_cursor_sequence=1
        )
        self.assertEqual(replay_run.duplicate_count, 1)
        self.assertEqual(metric.last_sync_run_id, replay_run)
        self.assertEqual(metric.current_revision_sequence, 1)

        empty_run = self._plan()
        self.sync_service._apply_performance_page(
            empty_run,
            PerformancePageDTO(
                reporting_context_hash=empty_run.reporting_context_hash,
                authoritative_complete=True,
            ),
            expected_cursor_sequence=2,
        )
        self.assertEqual(empty_run.state, "succeeded")
        self.assertEqual(
            self.env["marketing.center.metric.daily"].search_count(
                [("source_id", "=", self.source.id)]
            ),
            1,
        )
        self.assertEqual(metric.current_revision_sequence, 1)
        self.assertEqual(metric.last_sync_run_id, replay_run)
        self.assertNotEqual(metric.last_sync_run_id, empty_run)
        self.assertEqual(metric.last_sync_state, "succeeded")

    def test_counter_columns_are_postgresql_bigint(self):
        self.env.cr.execute(
            "SELECT table_name, column_name, data_type "
            "FROM information_schema.columns "
            "WHERE table_name IN (%s, %s) "
            "AND column_name IN (%s, %s, %s)",
            [
                "marketing_center_metric_daily",
                "marketing_center_metric_revision",
                "impressions",
                "clicks",
                "cost_micros",
            ],
        )
        columns = self.env.cr.fetchall()
        self.assertEqual(len(columns), 6)
        self.assertEqual({data_type for _, _, data_type in columns}, {"bigint"})

    def test_page_application_is_atomic(self):
        run = self._plan()
        page = PerformancePageDTO(
            items=(
                self._run_dto(run),
                self._run_dto(
                    run,
                    entity_external_ref="act_performance_lab/campaigns/invalid",
                    currency="EUR" if self.source.currency_id.name != "EUR" else "USD",
                ),
            ),
            reporting_context_hash=run.reporting_context_hash,
        )
        with self.assertRaises(ValidationError):
            self.sync_service._apply_performance_page(
                run, page, expected_cursor_sequence=0
            )
        self.assertFalse(
            self.env["marketing.center.metric.daily"].search(
                [("source_id", "=", self.source.id)]
            )
        )
        self.assertFalse(
            self.env["marketing.center.sync.cursor"].search(
                [("source_id", "=", self.source.id)]
            )
        )

    def test_reporting_context_is_retained_and_rejects_secrets(self):
        run = self._plan()
        self.assertEqual(
            run.reporting_context_json,
            {
                "contract_version": "test.performance.daily.v1",
                "grain": "campaign",
                "currency": self.source.currency_id.name,
                "report_timezone": "UTC",
                "level": "campaign",
                "time_increment": 1,
            },
        )
        self.sync_service._apply_performance_page(
            run,
            PerformancePageDTO(reporting_context_hash=run.reporting_context_hash),
            expected_cursor_sequence=0,
        )
        with self.assertRaises(ValidationError):
            self._plan(reporting_context={"access_token": "must-not-persist"})
        with self.assertRaises(ValidationError):
            self._plan(reporting_context={})

    def test_metric_runs_require_source_timezone_local_midnights(self):
        with self.assertRaises(ValidationError):
            self._plan(
                window_start=datetime.datetime(2026, 8, 30, 12, 0),
                window_end=datetime.datetime(2026, 8, 31, 12, 0),
            )
        with self.assertRaises(ValidationError):
            self._plan(
                window_start=datetime.datetime(2026, 8, 30, 0, 0),
                window_end=datetime.datetime(2026, 10, 1, 0, 0),
            )
        with self.assertRaises(ValidationError):
            self._plan(report_timezone="America/Sao_Paulo")

    def test_direct_ledger_mutation_is_blocked(self):
        result = self.performance_service._upsert_metric(
            self.env.company, self.source, self._dto()
        )
        metric = self.env["marketing.center.metric.daily"].browse(result.metric_id)
        with self.assertRaises(AccessError):
            metric.write({"clicks": 1})
        with self.assertRaises(AccessError):
            metric.current_revision_id.write({"clicks": 1})
        with self.assertRaises(AccessError):
            metric.unlink()
        wrong_grain_run = self._plan(
            grain="account",
            scope_ref="account:act_performance_lab:account",
        )
        with self.assertRaises(ValidationError):
            metric.with_context(
                marketing_performance_write_token=MARKETING_PERFORMANCE_WRITE_TOKEN
            ).write({"last_sync_run_id": wrong_grain_run.id})

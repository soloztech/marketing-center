import datetime
import uuid
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests.common import SavepointCase

from ..services.observability import (
    GOOGLE_CHANGE_RUN_ENTITY_TYPE,
    GoogleChangeObservationDTO,
    GoogleDiagnosticObservationDTO,
    GoogleObservationPage,
)
from .common import create_google_profile, project_google_source

_ADAPTER_PATH = (
    "odoo.addons.marketing_center_google.models.observability_sync."
    "GoogleMarketingReadAdapter"
)
_CHANGE_ROW_LIMIT_PATH = (
    "odoo.addons.marketing_center_google.models.observability_sync."
    "GOOGLE_CHANGE_ROW_LIMIT"
)


class TestGoogleObservabilitySync(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.identity, cls.profile = create_google_profile(
            cls.env, name="Google observability laboratory"
        )
        _customer, cls.source, cls.connection = project_google_source(
            cls.env,
            cls.profile,
            timezone="America/Sao_Paulo",
            login_customer_id="1111111111",
        )
        cls.change_service = cls.env["marketing.center.google.change.service"]
        cls.diagnostic_service = cls.env["marketing.center.google.diagnostic.service"]
        cls.observed_at = datetime.datetime(2026, 9, 1, 12, 0)

    def _plan_change(self, source=None, connection=None, trigger_ref=None):
        source = source or self.source
        connection = connection or self.connection
        run = self.change_service._plan_change(
            source,
            connection,
            local_date=datetime.date(2026, 8, 31),
            trigger_kind="manual",
            trigger_ref=trigger_ref or "test:%s" % uuid.uuid4(),
        )
        sequence = self.change_service._restart_cursor(run)
        self.change_service._enqueue_change_page(run, sequence)
        return run, sequence

    def _plan_diagnostic(self, source=None, connection=None, trigger_ref=None):
        source = source or self.source
        connection = connection or self.connection
        run = self.diagnostic_service._plan_diagnostics(
            source,
            connection,
            trigger_kind="manual",
            trigger_ref=trigger_ref or "test:%s" % uuid.uuid4(),
        )
        sequence = self.diagnostic_service._restart_cursor(run)
        self.diagnostic_service._enqueue_diagnostic_page(run, sequence)
        return run, sequence

    def _change(self, *, event_suffix="1", operation="update"):
        return GoogleChangeObservationDTO(
            event_ref=(
                "customers/1234567890/changeEvents/1700000000000000~%s~0" % event_suffix
            ),
            occurred_at=datetime.datetime(2026, 8, 31, 12, 15, 30),
            resource_type="campaign",
            resource_ref="customers/1234567890/campaigns/10",
            operation=operation,
            client_type="google_ads_web_client",
            changed_fields=("name", "status"),
            actor_hash="a" * 64,
            observed_at=self.observed_at,
        )

    def _diagnostic(self, *, primary_status="eligible"):
        return GoogleDiagnosticObservationDTO(
            asset_type="campaign",
            asset_ref="customers/1234567890/campaigns/10",
            configured_status="enabled",
            primary_status=primary_status,
            status_reasons=("budget_limited",) if primary_status == "limited" else (),
            observed_at=self.observed_at,
        )

    def _page(self, run, kind, items=()):
        return GoogleObservationPage(
            observation_kind=kind,
            items=tuple(items),
            reporting_context_hash=run.reporting_context_hash,
        )

    def _execute_change(self, run, sequence, pages):
        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_change_pages.return_value = pages
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_sync_google_change_page(sequence)
        return result, adapter_class

    def _execute_diagnostic(self, run, sequence, pages):
        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_diagnostic_pages.return_value = pages
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_sync_google_diagnostic_page(sequence)
        return result, adapter_class

    def test_observations_extend_source_identity_evidence_registry(self):
        registry = self.source._identity_evidence_registry()
        self.assertIn(
            ("marketing.center.google.change.observation", "source_id", ()),
            registry,
        )
        self.assertIn(
            ("marketing.center.google.diagnostic.observation", "source_id", ()),
            registry,
        )

    def test_cursor_mismatch_reschedules_each_observation_kind(self):
        cases = (
            ("change", self._plan_change, self._execute_change),
            ("diagnostic", self._plan_diagnostic, self._execute_diagnostic),
        )
        sync_service = self.env["marketing.center.sync.service"]
        for kind, planner, executor in cases:
            with self.subTest(kind=kind):
                run, expected_sequence = planner()
                authoritative_sequence = expected_sequence + 2
                old_job_uuid = run.queue_job_uuid
                cursor = sync_service._locked_cursor(run)
                sync_service._write_cursor(
                    cursor,
                    {"cursor_sequence": authoritative_sequence},
                )

                result, adapter_class = executor(run, expected_sequence, ())

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

    def test_change_ledger_is_immutable_and_replay_idempotent(self):
        item = self._change()
        first, sequence = self._plan_change()
        result, adapter_class = self._execute_change(
            first, sequence, (self._page(first, "change", (item,)),)
        )
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(
            adapter_class.call_args.kwargs["login_customer_id"], "1111111111"
        )
        observation = self.env["marketing.center.google.change.observation"].search(
            [("source_id", "=", self.source.id)]
        )
        self.assertEqual(len(observation), 1)

        replay, sequence = self._plan_change()
        self._execute_change(replay, sequence, (self._page(replay, "change", (item,)),))
        self.assertEqual(
            self.env["marketing.center.google.change.observation"].search_count(
                [("source_id", "=", self.source.id)]
            ),
            1,
        )
        self.assertEqual(replay.applied_count, 0)
        self.assertEqual(replay.duplicate_count, 1)
        with self.assertRaises(AccessError):
            observation.write({"operation": "remove"})
        with self.assertRaises(AccessError):
            observation.unlink()

    def test_same_change_event_with_different_content_fails_closed(self):
        first, sequence = self._plan_change()
        self._execute_change(
            first,
            sequence,
            (self._page(first, "change", (self._change(),)),),
        )
        conflict, sequence = self._plan_change()
        result, _adapter = self._execute_change(
            conflict,
            sequence,
            (
                self._page(
                    conflict,
                    "change",
                    (self._change(operation="remove"),),
                ),
            ),
        )
        self.assertEqual(result, {"state": "failed"})
        self.assertEqual(conflict.state, "failed")
        self.assertEqual(
            self.env["marketing.center.google.change.observation"].search_count(
                [("source_id", "=", self.source.id)]
            ),
            1,
        )

    def test_change_row_cap_accepts_below_and_exact_but_rejects_above(self):
        cases = (
            ("below", 1, "succeeded"),
            ("exact", 2, "succeeded"),
            ("above", 3, "failed"),
        )
        initial_count = self.env[
            "marketing.center.google.change.observation"
        ].search_count([("source_id", "=", self.source.id)])
        with patch(_CHANGE_ROW_LIMIT_PATH, 2):
            for case, item_count, expected_state in cases:
                run, sequence = self._plan_change()
                items = tuple(
                    self._change(event_suffix="cap-%s-%s" % (case, index))
                    for index in range(item_count)
                )
                result, _adapter = self._execute_change(
                    run,
                    sequence,
                    (self._page(run, "change", items),),
                )
                self.assertEqual(result["state"], expected_state)
                self.assertEqual(run.state, expected_state)

        self.assertEqual(
            self.env["marketing.center.google.change.observation"].search_count(
                [("source_id", "=", self.source.id)]
            ),
            initial_count + 3,
        )

    def test_diagnostics_keep_one_observation_per_asset_and_run(self):
        first, sequence = self._plan_diagnostic()
        eligible = self._diagnostic(primary_status="eligible")
        self._execute_diagnostic(
            first,
            sequence,
            (self._page(first, "diagnostic", (eligible,)),),
        )
        duplicate = self.env[
            "marketing.center.google.observation.service"
        ]._ingest_diagnostic(first, eligible)
        self.assertEqual(duplicate.disposition, "duplicate")

        second, sequence = self._plan_diagnostic()
        self._execute_diagnostic(
            second,
            sequence,
            (
                self._page(
                    second,
                    "diagnostic",
                    (self._diagnostic(primary_status="limited"),),
                ),
            ),
        )
        third, sequence = self._plan_diagnostic()
        self._execute_diagnostic(
            third,
            sequence,
            (self._page(third, "diagnostic", (eligible,)),),
        )
        observations = self.env[
            "marketing.center.google.diagnostic.observation"
        ].search([("source_id", "=", self.source.id)], order="id")
        self.assertEqual(len(observations), 3)
        self.assertEqual(
            observations.mapped("primary_status"), ["eligible", "limited", "eligible"]
        )

    def test_profile_rotation_during_io_discards_observation(self):
        run, sequence = self._plan_change()
        page = self._page(run, "change", (self._change(),))

        def rotate(*_args, **_kwargs):
            self.identity.write({"developer_token_ref": "GOOGLE_OBSERVATION_ROTATED"})
            return (page,)

        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_change_pages.side_effect = rotate
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_sync_google_change_page(sequence)
        self.assertEqual(result, {"stale": True})
        self.assertEqual(run.state, "stale")
        self.assertFalse(
            self.env["marketing.center.google.change.observation"].search(
                [("source_id", "=", self.source.id)]
            )
        )

    def test_daily_schedulers_are_idempotent_and_bounded(self):
        first_day = datetime.datetime(2026, 9, 1, 12, 0)
        first = self.change_service._cron_enqueue_google_changes(
            source_ids=[self.source.id], limit=1, now=first_day, lookback_days=2
        )
        second = self.change_service._cron_enqueue_google_changes(
            source_ids=[self.source.id], limit=1, now=first_day, lookback_days=2
        )
        self.assertEqual(first, {"queued": 2, "skipped": 0, "failed": 0})
        self.assertEqual(second, {"queued": 0, "skipped": 2, "failed": 0})
        first_day_runs = self.env["marketing.center.sync.run"].search(
            [
                ("source_id", "=", self.source.id),
                ("entity_type", "=", GOOGLE_CHANGE_RUN_ENTITY_TYPE),
                ("trigger_ref", "like", "changes:2026-09-01:%"),
            ]
        )
        self.assertEqual(len(first_day_runs), 2)
        for run in first_day_runs:
            sequence, _cursor_value = self.change_service._cursor_snapshot(run)
            result, _adapter = self._execute_change(
                run,
                sequence,
                (self._page(run, "change"),),
            )
            self.assertEqual(result["state"], "succeeded")

        next_day = datetime.datetime(2026, 9, 2, 12, 0)
        refresh = self.change_service._cron_enqueue_google_changes(
            source_ids=[self.source.id], limit=1, now=next_day, lookback_days=2
        )
        same_refresh = self.change_service._cron_enqueue_google_changes(
            source_ids=[self.source.id], limit=1, now=next_day, lookback_days=2
        )
        self.assertEqual(refresh, {"queued": 2, "skipped": 0, "failed": 0})
        self.assertEqual(same_refresh, {"queued": 0, "skipped": 2, "failed": 0})
        late_run = self.env["marketing.center.sync.run"].search(
            [
                ("source_id", "=", self.source.id),
                ("entity_type", "=", GOOGLE_CHANGE_RUN_ENTITY_TYPE),
                ("trigger_ref", "=", "changes:2026-09-02:2026-08-31"),
            ],
            limit=1,
        )
        self.assertTrue(late_run)
        late_sequence, _cursor_value = self.change_service._cursor_snapshot(late_run)
        result, _adapter = self._execute_change(
            late_run,
            late_sequence,
            (
                self._page(
                    late_run,
                    "change",
                    (self._change(event_suffix="late-next-day"),),
                ),
            ),
        )
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(
            self.env["marketing.center.google.change.observation"].search_count(
                [
                    ("source_id", "=", self.source.id),
                    ("event_ref", "like", "%~late-next-day~0"),
                ]
            ),
            1,
        )

        diagnostic_first = self.diagnostic_service._cron_enqueue_google_diagnostics(
            source_ids=[self.source.id], limit=1, now=first_day
        )
        diagnostic_second = self.diagnostic_service._cron_enqueue_google_diagnostics(
            source_ids=[self.source.id], limit=1, now=first_day
        )
        self.assertEqual(diagnostic_first, {"queued": 1, "skipped": 0, "failed": 0})
        self.assertEqual(diagnostic_second, {"queued": 0, "skipped": 1, "failed": 0})

    def test_manager_reads_only_observations_in_source_roster(self):
        visible_run, sequence = self._plan_change()
        self._execute_change(
            visible_run,
            sequence,
            (self._page(visible_run, "change", (self._change(),)),),
        )
        _customer, hidden_source, hidden_connection = project_google_source(
            self.env,
            self.profile,
            customer_id="9999999999",
            name="Hidden Google customer",
            timezone="America/Sao_Paulo",
            login_customer_id="1111111111",
        )
        hidden_run, sequence = self._plan_change(
            hidden_source, hidden_connection, trigger_ref="hidden:%s" % uuid.uuid4()
        )
        hidden_item = GoogleChangeObservationDTO(
            event_ref="customers/9999999999/changeEvents/1700000000000000~1~0",
            occurred_at=datetime.datetime(2026, 8, 31, 12, 15, 30),
            resource_type="campaign",
            resource_ref="customers/9999999999/campaigns/10",
            operation="update",
            client_type="google_ads_web_client",
            changed_fields=("status",),
            observed_at=self.observed_at,
        )
        self._execute_change(
            hidden_run,
            sequence,
            (self._page(hidden_run, "change", (hidden_item,)),),
        )

        manager_group = self.env.ref(
            "marketing_center_base.group_marketing_center_manager"
        )
        manager = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Google Observation Manager",
                    "login": "google-observation-manager-%s" % uuid.uuid4(),
                    "company_id": self.env.company.id,
                    "company_ids": [Command.set(self.env.company.ids)],
                    "groups_id": [Command.set(manager_group.ids)],
                }
            )
        )
        team = self.env["marketing.center.team"].create(
            {"name": "Google observation roster", "company_id": self.env.company.id}
        )
        self.env["marketing.center.team.member"].create(
            {"team_id": team.id, "user_id": manager.id, "role": "manager"}
        )
        self.env["marketing.center.team.source"].create(
            {
                "team_id": team.id,
                "source_id": self.source.id,
                "access_mode": "approve",
            }
        )
        manager_observations = (
            self.env["marketing.center.google.change.observation"]
            .with_user(manager)
            .search([])
        )
        self.assertEqual(manager_observations.source_id, self.source)
        with self.assertRaises(AccessError):
            manager_observations.read(["content_hash"])
        with self.assertRaises(AccessError):
            self.env["marketing.center.google.change.observation"].with_user(
                manager
            ).create({})

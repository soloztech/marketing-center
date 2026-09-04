import datetime
import uuid
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import SavepointCase

from odoo.addons.google_api_base.services.errors import (
    GoogleApiPermissionError,
    GoogleApiRateLimitError,
    GoogleApiTransientError,
)
from odoo.addons.marketing_center_base.services.catalog_dto import (
    ExternalEntityDTO,
    SyncPageDTO,
)

from ..services.catalog import encode_google_catalog_cursor
from .common import create_google_profile, project_google_source

_ADAPTER_PATH = (
    "odoo.addons.marketing_center_google.models.catalog_sync."
    "GoogleMarketingReadAdapter"
)


class TestGoogleCatalogSync(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.identity, cls.profile = create_google_profile(
            cls.env, name="Google catalog laboratory"
        )
        _customer, cls.source, cls.connection = project_google_source(
            cls.env,
            cls.profile,
            timezone="America/Sao_Paulo",
            login_customer_id="1111111111",
        )
        cls.service = cls.env["marketing.center.google.catalog.service"]
        cls.observed_at = datetime.datetime(2026, 9, 1, 12, 0)

    def _plan(self, *, trigger_kind="manual", trigger_ref=None):
        run = self.service._plan_sweep(
            self.source,
            self.connection,
            trigger_kind=trigger_kind,
            trigger_ref=trigger_ref or "test:%s" % uuid.uuid4(),
        )
        sequence = self.service._restart_cursor(run)
        self.service._enqueue_catalog_page(run, sequence)
        return run, sequence

    def _entity(self, external_ref="customers/1234567890/campaigns/10"):
        return ExternalEntityDTO(
            entity_type="campaign",
            external_ref=external_ref,
            external_id=external_ref.rsplit("/", 1)[-1],
            name="Search laboratory",
            remote_status="enabled",
            observed_at=self.observed_at,
            source_schema_version="google.ads.catalog.v25.2",
        )

    def _page(self, run, items=(), **values):
        return SyncPageDTO(
            items=items,
            reporting_context_hash=run.reporting_context_hash,
            **values,
        )

    def _execute(self, run, sequence, pages):
        job_uuid = run.queue_job_uuid
        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_catalog_pages.return_value = pages
            result = run.with_context(job_uuid=job_uuid)._job_sync_google_catalog_page(
                sequence
            )
        return result, adapter_class

    def test_cursor_mismatch_reschedules_exact_authoritative_page(self):
        run, expected_sequence = self._plan()
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
            )._job_sync_google_catalog_page(expected_sequence)

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

    def test_cursor_mismatch_and_profile_rotation_terminalizes_run(self):
        run, expected_sequence = self._plan()
        old_job_uuid = run.queue_job_uuid
        sync_service = self.env["marketing.center.sync.service"]
        cursor = sync_service._locked_cursor(run)
        sync_service._write_cursor(
            cursor,
            {"cursor_sequence": expected_sequence + 2},
        )
        service_class = type(self.service)
        original_lock = service_class._lock_execution_fences

        def rotate_before_lock(service, *args, **kwargs):
            self.profile.write(
                {"max_discovery_depth": self.profile.max_discovery_depth + 1}
            )
            return original_lock(service, *args, **kwargs)

        with patch.object(
            service_class,
            "_lock_execution_fences",
            rotate_before_lock,
        ):
            with patch(_ADAPTER_PATH) as adapter_class:
                result = run.with_context(
                    job_uuid=old_job_uuid
                )._job_sync_google_catalog_page(expected_sequence)

        run.invalidate_recordset(["queue_job_uuid", "state", "finished_at"])
        self.assertEqual(result, {"stale": True})
        self.assertEqual(run.state, "stale")
        self.assertTrue(run.finished_at)
        adapter_class.assert_not_called()

    def test_catalog_page_projects_and_replay_is_idempotent(self):
        entity = self._entity()
        first, sequence = self._plan()
        result, _adapter = self._execute(
            first, sequence, (self._page(first, (entity,)),)
        )
        self.assertEqual(result["state"], "succeeded")
        current = self.env["marketing.center.external.entity"].search(
            [
                ("source_id", "=", self.source.id),
                ("external_ref", "=", entity.external_ref),
            ]
        )
        self.assertEqual(len(current), 1)
        revision = current.current_revision_id

        replay, sequence = self._plan(trigger_kind="retry")
        self._execute(replay, sequence, (self._page(replay, (entity,)),))
        current.invalidate_recordset()
        self.assertEqual(current.current_revision_id, revision)
        self.assertEqual(replay.duplicate_count, 1)
        self.assertEqual(replay.applied_count, 0)

    def test_provider_pagination_uses_opaque_token_and_cursor_cas(self):
        run, sequence = self._plan()
        first_cursor = encode_google_catalog_cursor("campaign", "opaque-page-2")
        first, _adapter = self._execute(
            run,
            sequence,
            (self._page(run, next_cursor=first_cursor, has_more=True),),
        )
        self.assertTrue(first["has_more"])
        successor_uuid = run.queue_job_uuid
        self.assertEqual(
            self.service._cursor_snapshot(run), (sequence + 1, first_cursor)
        )

        terminal, adapter_class = self._execute(
            run,
            sequence + 1,
            (self._page(run),),
        )
        self.assertEqual(terminal["state"], "succeeded")
        call = adapter_class.return_value.fetch_catalog_pages.call_args
        self.assertEqual(
            adapter_class.call_args.kwargs["login_customer_id"],
            "1111111111",
        )
        self.assertEqual(call.args, (self.source.external_account_id, "campaign"))
        self.assertEqual(call.kwargs["page_token"], "opaque-page-2")
        self.assertEqual(run.queue_job_uuid, successor_uuid)

    def test_new_sweep_always_resets_provider_page_token(self):
        run, sequence = self._plan()
        poisoned = encode_google_catalog_cursor("campaign", "poisoned-token")
        self._execute(
            run,
            sequence,
            (self._page(run, next_cursor=poisoned, has_more=True),),
        )
        self.service._finish_failure(
            run,
            run.queue_job_uuid,
            classification="permanent",
            summary="Synthetic predecessor failure.",
        )
        replacement, sequence = self._plan(trigger_kind="retry")
        _result, adapter_class = self._execute(
            replacement,
            sequence,
            (self._page(replacement),),
        )
        call = adapter_class.return_value.fetch_catalog_pages.call_args
        self.assertEqual(call.args[1], "campaign")
        self.assertEqual(call.kwargs["page_token"], "")

    def test_identity_rotation_during_io_discards_page(self):
        run, sequence = self._plan()
        page = self._page(run, (self._entity(),))

        def rotate(*_args, **_kwargs):
            self.identity.write({"developer_token_ref": "GOOGLE_ROTATED_TOKEN"})
            return (page,)

        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_catalog_pages.side_effect = rotate
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_sync_google_catalog_page(sequence)
        self.assertEqual(result, {"stale": True})
        self.assertEqual(run.state, "stale")
        self.assertFalse(
            self.env["marketing.center.external.entity"].search(
                [("source_id", "=", self.source.id)]
            )
        )

    def test_source_external_id_cannot_change_during_provider_io(self):
        run, sequence = self._plan()
        page = self._page(run, (self._entity(),))

        def attempt_identity_change(*_args, **_kwargs):
            with self.assertRaises(AccessError):
                self.source.write({"external_account_id": "9999999999"})
            return (page,)

        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_catalog_pages.side_effect = (
                attempt_identity_change
            )
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_sync_google_catalog_page(sequence)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(self.source.external_account_id, "1234567890")

    def test_provider_io_happens_before_execution_row_locks(self):
        run, sequence = self._plan()
        page = self._page(run, (self._entity(),))
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
                adapter_class.return_value.fetch_catalog_pages.side_effect = (
                    provider_page
                )
                result = run.with_context(
                    job_uuid=run.queue_job_uuid
                )._job_sync_google_catalog_page(sequence)
        self.assertEqual(result["state"], "succeeded")
        self.assertTrue(lock_calls)

    def test_quota_cooldown_persists_without_advancing_cursor(self):
        run, sequence = self._plan()
        original_job_uuid = run.queue_job_uuid
        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_catalog_pages.side_effect = (
                GoogleApiRateLimitError(
                    "synthetic quota",
                    retry_after_seconds=137,
                    provider_status="RESOURCE_EXHAUSTED",
                )
            )
            result = run.with_context(
                job_uuid=original_job_uuid
            )._job_sync_google_catalog_page(sequence)
        run.invalidate_recordset(["queue_job_uuid", "state", "deferred_until"])
        self.connection.invalidate_recordset(
            ["cooldown_until", "health_state", "last_health_error_class"]
        )
        self.assertEqual(
            result,
            {"rescheduled": True, "retry_after": 137, "quota_attempt": 1},
        )
        self.assertNotEqual(run.queue_job_uuid, original_job_uuid)
        successor = (
            self.env["queue.job"]
            .sudo()
            .search([("uuid", "=", run.queue_job_uuid)], limit=1)
        )
        self.assertTrue(successor)
        self.assertTrue(successor.eta)
        self.assertTrue(run.deferred_until)
        self.assertEqual(self.connection.health_state, "degraded")
        self.assertEqual(self.connection.last_health_error_class, "rate_limited")
        self.assertTrue(self.connection.cooldown_until)
        self.assertEqual(self.service._cursor_snapshot(run)[0], sequence)
        self.assertEqual(run.state, "queued")

    def test_connection_cooldown_is_monotonic_and_late_success_preserves_it(self):
        now = fields.Datetime.now()
        self.service._mark_connection_cooldown(self.connection, 3600)
        self.connection.invalidate_recordset(
            ["cooldown_until", "health_state", "last_health_error_class"]
        )
        first_deadline = self.connection.cooldown_until
        self.assertGreater(first_deadline, now)

        self.service._mark_connection_cooldown(self.connection, 60)
        self.connection.invalidate_recordset(
            ["cooldown_until", "health_state", "last_health_error_class"]
        )
        self.assertEqual(self.connection.cooldown_until, first_deadline)

        self.service._mark_connection_success(self.connection)
        self.connection.invalidate_recordset(
            ["cooldown_until", "health_state", "last_health_error_class"]
        )
        self.assertEqual(self.connection.cooldown_until, first_deadline)
        self.assertEqual(self.connection.health_state, "degraded")
        self.assertEqual(self.connection.last_health_error_class, "rate_limited")

    def test_quota_retry_chain_has_a_terminal_ceiling(self):
        run, sequence = self._plan()
        job_uuid = run.queue_job_uuid
        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_catalog_pages.side_effect = (
                GoogleApiRateLimitError(
                    "synthetic quota ceiling",
                    retry_after_seconds=60,
                    provider_status="RESOURCE_EXHAUSTED",
                )
            )
            result = run.with_context(job_uuid=job_uuid)._job_sync_google_catalog_page(
                sequence, 8
            )
        self.assertEqual(result, {"state": "failed"})
        self.assertEqual(run.state, "failed")
        self.assertEqual(run.page_count, 0)
        self.assertEqual(self.service._cursor_snapshot(run)[0], sequence)

    def test_provider_retry_after_uses_deferred_successor_and_watchdog_fence(self):
        run, sequence = self._plan()
        original_job_uuid = run.queue_job_uuid
        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_catalog_pages.side_effect = (
                GoogleApiTransientError(
                    "synthetic provider maintenance",
                    retry_after_seconds=86_400,
                    provider_status="UNAVAILABLE",
                )
            )
            result = run.with_context(
                job_uuid=original_job_uuid
            )._job_sync_google_catalog_page(sequence)
        run.invalidate_recordset(
            ["queue_job_uuid", "state", "deferred_until", "error_class"]
        )
        self.connection.invalidate_recordset(
            ["cooldown_until", "health_state", "last_health_error_class"]
        )
        self.assertEqual(
            result,
            {"rescheduled": True, "retry_after": 86_400, "quota_attempt": 1},
        )
        self.assertNotEqual(run.queue_job_uuid, original_job_uuid)
        self.assertTrue(run.deferred_until)
        self.assertEqual(self.connection.last_health_error_class, "transient")

        protected = self.env["marketing.center.sync.service"]._recover_stuck_runs(
            now=run.deferred_until + datetime.timedelta(hours=5)
        )
        run.invalidate_recordset(["state"])
        self.assertEqual(protected, {"cancelled": 0, "failed": 0, "processed": 0})
        self.assertEqual(run.state, "queued")

    def test_permission_failure_pauses_profile_and_connection(self):
        run, sequence = self._plan()
        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_catalog_pages.side_effect = (
                GoogleApiPermissionError(
                    "synthetic revoked access",
                    provider_status="PERMISSION_DENIED",
                    provider_reason="USER_PERMISSION_DENIED",
                )
            )
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_sync_google_catalog_page(sequence)
        self.assertEqual(result, {"stale": True, "profile_paused": True})
        self.assertEqual(run.state, "stale")
        self.assertEqual(self.profile.health_state, "unhealthy")
        self.assertEqual(self.connection.state, "paused")
        self.assertEqual(self.connection.last_health_error_class, "permission_denied")
        self.assertEqual(run.page_count, 0)

    def test_daily_catalog_cron_is_idempotent_and_bounded(self):
        now = datetime.datetime(2026, 9, 1, 12, 0)
        first = self.service._cron_enqueue_google_catalog(
            source_ids=[self.source.id], limit=1, now=now
        )
        second = self.service._cron_enqueue_google_catalog(
            source_ids=[self.source.id], limit=1, now=now
        )
        self.assertEqual(first, {"queued": 1, "skipped": 0, "failed": 0})
        self.assertEqual(second, {"queued": 0, "skipped": 1, "failed": 0})

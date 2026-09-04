import datetime
import uuid
from unittest.mock import patch

from odoo import _, fields
from odoo.exceptions import ValidationError
from odoo.tests.common import SavepointCase

from ..models.catalog_service import MarketingCenterCatalogService
from ..services.catalog_dto import ExternalEntityDTO, SyncPageDTO


class TestMarketingSyncService(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = cls.env["marketing.center.source"].create(
            {
                "name": "Sync source",
                "company_id": cls.env.company.id,
                "service": "meta.ads",
                "external_account_ref": "act_sync_lab",
                "currency_id": cls.env.company.currency_id.id,
                "timezone": "UTC",
                "state": "active",
            }
        )
        cls.connection = cls.env["marketing.center.connection"].create(
            {
                "name": "Sync reader",
                "source_id": cls.source.id,
                "adapter_key": "meta.graph",
                "purpose": "reader",
                "profile_public_ref": "profile-sync-reader",
                "profile_revision": 2,
                "state": "ready",
            }
        )
        cls.service = cls.env["marketing.center.sync.service"]

    def _plan(self, trigger_ref=None, **overrides):
        values = {
            "sync_kind": "catalog",
            "entity_type": "campaign",
            "scope_ref": "account:act_sync_lab",
            "trigger_kind": "manual",
            "trigger_ref": trigger_ref or str(uuid.uuid4()),
            "reporting_context": {"catalog_version": 1},
        }
        values.update(overrides)
        return self.service._plan_run(
            self.env.company,
            self.source,
            self.connection,
            **values,
        )

    def _page(self, run, **overrides):
        values = {"reporting_context_hash": run.reporting_context_hash}
        values.update(overrides)
        return SyncPageDTO(**values)

    def _entity(self, name="Campaign Sync", external_id="100"):
        return ExternalEntityDTO(
            entity_type="campaign",
            external_ref="act_sync_lab/campaigns/%s" % external_id,
            external_id=external_id,
            name=name,
            remote_status="active",
            observed_at=datetime.datetime(2026, 8, 30, 12, 0),
        )

    def test_run_and_page_replay_are_idempotent(self):
        trigger_ref = str(uuid.uuid4())
        run = self._plan(trigger_ref)
        self.assertEqual(self._plan(trigger_ref), run)
        page = self._page(
            run,
            items=(self._entity(),),
            provider_request_id="request-1",
            authoritative_complete=True,
        )
        self.service._apply_entity_page(run, page, expected_cursor_sequence=0)
        self.assertEqual(run.state, "succeeded")
        self.assertEqual(run.applied_count, 1)
        cursor = self.env["marketing.center.sync.cursor"].search(
            [("source_id", "=", self.source.id)], limit=1
        )
        self.assertEqual(cursor.cursor_sequence, 1)

        replay_run = self._plan(str(uuid.uuid4()))
        self.service._apply_entity_page(replay_run, page, expected_cursor_sequence=1)
        self.assertEqual(replay_run.state, "succeeded")
        self.assertEqual(replay_run.duplicate_count, 1)
        self.assertEqual(cursor.cursor_sequence, 2)
        self.assertEqual(
            self.env["marketing.center.external.entity"].search_count(
                [("source_id", "=", self.source.id)]
            ),
            1,
        )

    def test_async_provider_job_does_not_advance_cursor(self):
        run = self._plan()
        page = self._page(
            run,
            provider_job_ref="provider-job-1",
            provider_job_state="running",
            has_more=True,
        )
        self.service._apply_entity_page(run, page, expected_cursor_sequence=0)
        cursor = self.env["marketing.center.sync.cursor"].search(
            [("source_id", "=", self.source.id)], limit=1
        )
        self.assertEqual(run.state, "running")
        self.assertEqual(cursor.cursor_sequence, 1)
        self.assertEqual(cursor.provider_job_ref, "provider-job-1")

    def test_connection_rotation_marks_planned_run_stale(self):
        run = self._plan()
        self.connection.write({"profile_revision": 3})
        self.service._apply_entity_page(
            run,
            self._page(run, items=(self._entity(name="Stale"),)),
            expected_cursor_sequence=0,
        )
        self.assertEqual(run.state, "stale")

    def test_source_pause_marks_planned_run_stale(self):
        run = self._plan()
        self.source.write({"state": "paused"})
        self.service._apply_entity_page(
            run,
            self._page(run, items=(self._entity(name="Stale source"),)),
            expected_cursor_sequence=0,
        )
        self.assertEqual(run.state, "stale")
        self.assertFalse(
            self.env["marketing.center.external.entity"].search(
                [("name", "=", "Stale source")]
            )
        )

    def test_page_context_and_cursor_sequence_are_fenced(self):
        run = self._plan()
        with self.assertRaises(ValidationError):
            self.service._apply_entity_page(
                run,
                SyncPageDTO(items=(self._entity(name="Wrong context"),)),
                expected_cursor_sequence=0,
            )
        with self.assertRaises(ValidationError):
            self.service._apply_entity_page(
                run,
                self._page(run, items=(self._entity(name="Wrong sequence"),)),
                expected_cursor_sequence=1,
            )
        self.assertFalse(
            self.env["marketing.center.external.entity"].search(
                [("name", "in", ["Wrong context", "Wrong sequence"])]
            )
        )

    def test_catalog_page_rolls_back_every_projection_on_partial_failure(self):
        run = self._plan()
        page = self._page(
            run,
            items=(
                self._entity(name="Must roll back", external_id="atomic-1"),
                self._entity(name="Synthetic failure", external_id="atomic-2"),
            ),
        )
        original_upsert = MarketingCenterCatalogService._upsert_entity

        def fail_second(service, company, source, payload, sync_run=None):
            if payload.external_id == "atomic-2":
                raise ValidationError(_("Synthetic second-item failure"))
            return original_upsert(
                service,
                company,
                source,
                payload,
                sync_run=sync_run,
            )

        with patch.object(
            MarketingCenterCatalogService,
            "_upsert_entity",
            new=fail_second,
        ):
            with self.assertRaises(ValidationError):
                self.service._apply_entity_page(
                    run,
                    page,
                    expected_cursor_sequence=0,
                )

        run.invalidate_recordset(["state", "page_count"])
        self.assertEqual(run.state, "planned")
        self.assertEqual(run.page_count, 0)
        self.assertFalse(
            self.env["marketing.center.external.entity"].search(
                [("external_id", "in", ["atomic-1", "atomic-2"])]
            )
        )
        self.assertFalse(
            self.env["marketing.center.sync.cursor"].search(
                [("source_id", "=", self.source.id)]
            )
        )

    def test_cursor_sequence_is_mandatory_and_strict(self):
        run = self._plan()
        page = self._page(run)
        with self.assertRaises(TypeError):
            self.service._apply_entity_page(run, page)
        for invalid in (None, True, -1):
            with self.assertRaises(ValidationError):
                self.service._apply_entity_page(
                    run,
                    page,
                    expected_cursor_sequence=invalid,
                )

    def test_locked_cursor_invalidates_every_preloaded_mutable_field(self):
        run = self._plan()
        cursor = self.service._locked_cursor(run)
        mutable_fields = (
            "cursor_sequence",
            "cursor_value",
            "cursor_digest",
            "provider_job_ref",
            "watermark",
            "last_success_run_id",
            "last_advanced_at",
        )
        preloaded = tuple(cursor[field_name] for field_name in mutable_fields)
        self.assertEqual(preloaded[0], 0)
        self.assertFalse(any(preloaded[1:]))
        advanced_at = fields.Datetime.now().replace(microsecond=0)
        self.env.cr.execute(
            "UPDATE marketing_center_sync_cursor "
            "SET cursor_sequence = %s, cursor_value = %s, cursor_digest = %s, "
            "provider_job_ref = %s, watermark = %s, last_success_run_id = %s, "
            "last_advanced_at = %s WHERE id = %s",
            [
                7,
                "provider-next-page",
                "a" * 64,
                "provider-job-7",
                "watermark-7",
                run.id,
                advanced_at,
                cursor.id,
            ],
        )

        locked_cursor = self.service._locked_cursor(run)

        self.assertEqual(locked_cursor, cursor)
        self.assertEqual(
            tuple(locked_cursor[field_name] for field_name in mutable_fields),
            (
                7,
                "provider-next-page",
                "a" * 64,
                "provider-job-7",
                "watermark-7",
                run,
                advanced_at,
            ),
        )

    def test_active_scope_and_idempotency_conflicts_are_explicit(self):
        run = self._plan()
        with self.assertRaises(ValidationError):
            self._plan()
        with self.assertRaises(ValidationError):
            self._plan(
                run.trigger_ref,
                window_start=datetime.datetime(2026, 8, 29, 0, 0),
                window_end=datetime.datetime(2026, 8, 30, 0, 0),
            )

    def test_watchdog_releases_planned_and_active_scopes(self):
        planned = self._plan()
        future = fields.Datetime.now() + datetime.timedelta(minutes=31)
        result = self.service._recover_stuck_runs(now=future)
        planned.invalidate_recordset(["state", "error_class"])
        self.assertEqual(result, {"cancelled": 1, "failed": 0, "processed": 1})
        self.assertEqual(planned.state, "cancelled")
        self.assertEqual(planned.error_class, "orphaned")

        queued = self._plan()
        self.service._transition(queued, "queued")
        result = self.service._recover_stuck_runs(
            now=fields.Datetime.now() + datetime.timedelta(hours=7)
        )
        queued.invalidate_recordset(["state", "error_class"])
        self.assertEqual(result, {"cancelled": 0, "failed": 1, "processed": 1})
        self.assertEqual(queued.state, "failed")
        self.assertEqual(queued.error_class, "orphaned")

    def test_watchdog_uses_recent_progress_not_only_start_time(self):
        queued = self._plan()
        self.service._transition(queued, "queued")
        now = fields.Datetime.now()
        self.env.cr.execute(
            "UPDATE marketing_center_sync_run "
            "SET started_at = %s, write_date = %s WHERE id = %s",
            [
                now - datetime.timedelta(hours=24),
                now - datetime.timedelta(minutes=5),
                queued.id,
            ],
        )
        queued.invalidate_recordset(["started_at", "write_date"])
        result = self.service._recover_stuck_runs(now=now)
        queued.invalidate_recordset(["state"])
        self.assertEqual(result, {"cancelled": 0, "failed": 0, "processed": 0})
        self.assertEqual(queued.state, "queued")

    def test_watchdog_respects_deferred_successor_eta_then_recovers_it(self):
        queued = self._plan()
        now = fields.Datetime.now()
        deferred_until = now + datetime.timedelta(hours=24)
        self.service._transition(
            queued,
            "queued",
            {"deferred_until": deferred_until},
        )
        self.env.cr.execute(
            "UPDATE marketing_center_sync_run "
            "SET started_at = %s, write_date = %s WHERE id = %s",
            [
                now - datetime.timedelta(hours=24),
                now - datetime.timedelta(hours=24),
                queued.id,
            ],
        )
        queued.invalidate_recordset(["started_at", "write_date", "deferred_until"])

        protected = self.service._recover_stuck_runs(
            now=deferred_until + datetime.timedelta(hours=5)
        )
        queued.invalidate_recordset(["state"])
        self.assertEqual(protected, {"cancelled": 0, "failed": 0, "processed": 0})
        self.assertEqual(queued.state, "queued")

        recovered = self.service._recover_stuck_runs(
            now=deferred_until + datetime.timedelta(hours=7)
        )
        queued.invalidate_recordset(["state", "error_class"])
        self.assertEqual(recovered, {"cancelled": 0, "failed": 1, "processed": 1})
        self.assertEqual(queued.state, "failed")
        self.assertEqual(queued.error_class, "orphaned")

    def test_explicit_cancel_is_idempotent_and_fences_late_pages(self):
        run = self._plan()
        self.service._transition(run, "queued")
        self.service._cancel_run(run)
        self.service._cancel_run(run)
        self.assertEqual(run.state, "cancelled")
        with self.assertRaises(ValidationError):
            self.service._apply_entity_page(
                run,
                self._page(run, items=(self._entity(name="Too late"),)),
                expected_cursor_sequence=0,
            )
        self.assertFalse(
            self.env["marketing.center.external.entity"].search(
                [("name", "=", "Too late")]
            )
        )

    def test_distinct_windows_have_distinct_cursor_ownership(self):
        first_run = self._plan(
            window_start=datetime.datetime(2026, 8, 28, 0, 0),
            window_end=datetime.datetime(2026, 8, 29, 0, 0),
        )
        self.service._apply_entity_page(
            first_run,
            self._page(first_run),
            expected_cursor_sequence=0,
        )
        second_run = self._plan(
            window_start=datetime.datetime(2026, 8, 29, 0, 0),
            window_end=datetime.datetime(2026, 8, 30, 0, 0),
        )
        self.service._apply_entity_page(
            second_run,
            self._page(second_run),
            expected_cursor_sequence=0,
        )
        self.assertNotEqual(first_run.window_key, second_run.window_key)
        cursors = self.env["marketing.center.sync.cursor"].search(
            [("source_id", "=", self.source.id)]
        )
        self.assertEqual(len(cursors), 2)
        self.assertEqual(set(cursors.mapped("cursor_sequence")), {1})

    def test_errors_from_an_earlier_page_keep_terminal_run_partial(self):
        run = self._plan()
        first_page = self._page(
            run,
            next_cursor="page-2",
            has_more=True,
            errors=({"code": "partial_item"},),
        )
        self.service._apply_entity_page(
            run,
            first_page,
            expected_cursor_sequence=0,
        )
        self.service._apply_entity_page(
            run,
            self._page(run),
            expected_cursor_sequence=1,
        )
        self.assertEqual(run.state, "partial")
        self.assertEqual(run.error_count, 1)

    def test_provider_job_and_watermark_accept_dto_limits(self):
        run = self._plan()
        page = self._page(
            run,
            provider_job_ref="j" * 1024,
            provider_job_state="running",
            watermark="w" * 4096,
            has_more=True,
        )
        self.service._apply_entity_page(
            run,
            page,
            expected_cursor_sequence=0,
        )
        cursor = self.env["marketing.center.sync.cursor"].search(
            [("source_id", "=", self.source.id)], limit=1
        )
        self.assertEqual(len(cursor.provider_job_ref), 1024)
        self.assertEqual(len(cursor.watermark), 4096)

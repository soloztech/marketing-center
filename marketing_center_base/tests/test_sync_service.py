import datetime
import uuid

from odoo.exceptions import ValidationError
from odoo.tests.common import SavepointCase

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

    def _entity(self, name="Campaign Sync"):
        return ExternalEntityDTO(
            entity_type="campaign",
            external_ref="act_sync_lab/campaigns/100",
            external_id="100",
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

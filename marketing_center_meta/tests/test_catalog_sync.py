import datetime
import uuid
from unittest.mock import patch

from odoo.tests.common import SavepointCase

from odoo.addons.marketing_center_base.services.catalog_dto import (
    ExternalEntityDTO,
    SyncPageDTO,
    sha256_text,
)
from odoo.addons.marketing_center_base.services.tokens import MARKETING_SYNC_WRITE_TOKEN
from odoo.addons.meta_api_base.services.errors import (
    MetaApiPausedError,
    MetaApiTransientError,
)
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.adapter import MetaAdAccount
from .common import create_meta_profile

_ADAPTER_PATH = (
    "odoo.addons.marketing_center_meta.models.catalog_sync.MetaMarketingReadAdapter"
)


class TestMetaCatalogSync(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.meta_app, cls.profile = create_meta_profile(
            cls.env,
            name="Meta catalog laboratory",
            external_app_id="123456789",
            app_secret_ref="ODOO_META_CATALOG_APP_SECRET",
            access_token_ref="ODOO_META_CATALOG_READER_TOKEN",
        )
        account = MetaAdAccount(
            external_ref="act_123",
            external_id="123",
            name="Meta Catalog Laboratory",
            currency=cls.env.company.currency_id.name,
            timezone="America/Sao_Paulo",
            account_status=1,
            disable_reason=0,
        )
        capabilities = {
            "read_entities": True,
            "read_metrics": True,
            "receive_leads": False,
        }
        cls.source = cls.env["marketing.center.meta.service"]._upsert_source(
            cls.profile,
            capabilities,
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
        cls.observed_at = datetime.datetime(2026, 8, 31, 12, 0)

    def _plan(self):
        run = self.service._plan_sweep(
            self.source,
            self.connection,
            trigger_kind="manual",
            trigger_ref="test:%s" % uuid.uuid4(),
        )
        cursor_sequence = self.service._restart_catalog_cursor(run)
        self.service._enqueue_page(run, cursor_sequence)
        return run

    def _page(self, run, items=(), **values):
        return SyncPageDTO(
            items=items,
            reporting_context_hash=run.reporting_context_hash,
            **values,
        )

    def _entity(self, entity_type, external_ref, **values):
        payload = {
            "entity_type": entity_type,
            "external_ref": external_ref,
            "external_id": external_ref.rsplit("/", 1)[-1],
            "name": "%s %s" % (entity_type, external_ref.rsplit("/", 1)[-1]),
            "observed_at": self.observed_at,
            "source_schema_version": "meta.marketing.catalog.v1",
        }
        payload.update(values)
        return ExternalEntityDTO(**payload)

    def _execute(self, run, sequence, provider_page):
        job_uuid = run.queue_job_uuid
        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_catalog_page.return_value = provider_page
            result = run.with_context(job_uuid=job_uuid)._job_sync_meta_catalog_page(
                sequence
            )
        return result, adapter_class

    def test_single_run_orders_hierarchy_and_replaces_page_job_uuid(self):
        old = self.env["marketing.center.catalog.service"]._upsert_entity(
            self.env.company,
            self.source,
            self._entity("campaign", "act_123/campaigns/9"),
        )
        run = self._plan()
        first_job_uuid = run.queue_job_uuid
        pages = (
            self._page(
                run,
                (self._entity("campaign", "act_123/campaigns/10"),),
            ),
            self._page(
                run,
                (
                    self._entity(
                        "group",
                        "act_123/adsets/20",
                        group_type="meta_adset",
                        parent_entity_type="campaign",
                        parent_external_ref="act_123/campaigns/10",
                    ),
                ),
            ),
            self._page(
                run,
                (
                    self._entity(
                        "ad",
                        "act_123/ads/30",
                        parent_entity_type="group",
                        parent_external_ref="act_123/adsets/20",
                        attributes={"meta.creative_ref": "act_123/creatives/40"},
                    ),
                ),
            ),
            self._page(
                run,
                (self._entity("creative", "act_123/creatives/40"),),
            ),
        )
        observed_stages = []
        for sequence, page in enumerate(pages):
            result, adapter_class = self._execute(run, sequence, page)
            observed_stages.append(
                adapter_class.return_value.fetch_catalog_page.call_args.args[1]
            )
            if sequence == 0:
                self.assertEqual(run.state, "running")
                self.assertNotEqual(run.queue_job_uuid, first_job_uuid)
        self.assertEqual(observed_stages, ["campaign", "group", "ad", "creative"])
        self.assertEqual(run.state, "succeeded")
        self.assertEqual(run.page_count, 4)
        entities = self.env["marketing.center.external.entity"].search(
            [("source_id", "=", self.source.id)]
        )
        campaign = entities.filtered(
            lambda item: item.external_ref == "act_123/campaigns/10"
        )
        group = entities.filtered(lambda item: item.external_ref == "act_123/adsets/20")
        ad = entities.filtered(lambda item: item.external_ref == "act_123/ads/30")
        creative = entities.filtered(
            lambda item: item.external_ref == "act_123/creatives/40"
        )
        self.assertEqual(group.parent_id, campaign)
        self.assertEqual(ad.parent_id, group)
        self.assertFalse(creative.parent_id)
        old_entity = self.env["marketing.center.external.entity"].browse(old.entity_id)
        self.assertFalse(old_entity.remote_missing_at)
        self.assertFalse(old_entity.current_revision_id.is_tombstone)
        self.assertFalse(result["has_more"])

    def test_provider_pages_advance_same_stage_by_core_cursor_cas(self):
        run = self._plan()
        first_page = self._page(run, next_cursor="opaque-b", has_more=True)
        first, _adapter = self._execute(run, 0, first_page)
        first_successor = run.queue_job_uuid
        self.assertTrue(first["has_more"])
        self.assertEqual(self.service._cursor_snapshot(run)[0], 1)

        terminal_page = self._page(run)
        second, adapter_class = self._execute(run, 1, terminal_page)
        call = adapter_class.return_value.fetch_catalog_page.call_args
        self.assertEqual(call.args[1], "campaign")
        self.assertEqual(call.kwargs["after"], "opaque-b")
        self.assertNotEqual(run.queue_job_uuid, first_successor)
        self.assertTrue(second["has_more"])
        self.assertEqual(self.service._cursor_snapshot(run)[0], 2)

    def test_cursor_mismatch_reschedules_exact_authoritative_page(self):
        run = self._plan()
        expected_sequence = self.service._cursor_snapshot(run)[0]
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
            )._job_sync_meta_catalog_page(expected_sequence)

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

    def test_profile_rotation_during_io_discards_page(self):
        run = self._plan()
        page = self._page(
            run,
            (self._entity("campaign", "act_123/campaigns/rotated"),),
        )

        def rotate(*_args, **_kwargs):
            self.profile.write(
                {"access_token_ref": "ODOO_META_CATALOG_READER_TOKEN_ROTATED"}
            )
            return page

        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_catalog_page.side_effect = rotate
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_sync_meta_catalog_page(0)
        self.assertEqual(result, {"stale": True})
        self.assertEqual(run.state, "stale")
        self.assertFalse(
            self.env["marketing.center.external.entity"].search(
                [("external_ref", "=", "act_123/campaigns/rotated")]
            )
        )

    def test_orphan_job_never_crosses_provider_boundary(self):
        run = self._plan()
        with patch(_ADAPTER_PATH) as adapter_class:
            result = run.with_context(
                job_uuid=str(uuid.uuid4())
            )._job_sync_meta_catalog_page(0)
        self.assertEqual(result, {"orphan": True})
        adapter_class.assert_not_called()
        self.assertEqual(run.state, "queued")

    def test_transient_retry_commits_terminal_failure_at_ceiling(self):
        run = self._plan()
        job = (
            self.env["queue.job"]
            .sudo()
            .search([("uuid", "=", run.queue_job_uuid)], limit=1)
        )
        error = MetaApiTransientError("synthetic transient")
        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_catalog_page.side_effect = error
            with self.assertRaises(RetryableJobError):
                run.with_context(
                    job_uuid=run.queue_job_uuid
                )._job_sync_meta_catalog_page(0)
            self.assertEqual(run.state, "queued")
            job.sudo().write({"retry": 7})
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_sync_meta_catalog_page(0)
        self.assertEqual(result, {"state": "failed"})
        self.assertEqual(run.state, "failed")
        self.assertEqual(run.error_class, "transient")
        self.assertFalse(
            self.env["marketing.center.external.entity"].search(
                [("source_id", "=", self.source.id)]
            )
        )
        replacement = self.service._plan_sweep(
            self.source,
            self.connection,
            trigger_kind="retry",
            trigger_ref="retry:%s" % uuid.uuid4(),
        )
        self.assertEqual(replacement.state, "planned")

    def test_invalid_cursor_closes_run_and_releases_scope(self):
        run = self._plan()
        self._execute(
            run,
            0,
            self._page(run, next_cursor="valid-next", has_more=True),
        )
        cursor = self.env["marketing.center.sync.cursor"].search(
            [("source_id", "=", self.source.id)], limit=1
        )
        cursor.with_context(
            marketing_sync_write_token=MARKETING_SYNC_WRITE_TOKEN
        ).write(
            {
                "cursor_value": "not-json",
                "cursor_digest": sha256_text("not-json"),
            }
        )
        result = run.with_context(
            job_uuid=run.queue_job_uuid
        )._job_sync_meta_catalog_page(1)
        self.assertEqual(result, {"state": "failed"})
        self.assertEqual(run.state, "failed")
        replacement = self.service._plan_sweep(
            self.source,
            self.connection,
            trigger_kind="retry",
            trigger_ref="cursor-retry:%s" % uuid.uuid4(),
        )
        self.assertEqual(replacement.state, "planned")

    def test_new_full_sweep_discards_failed_predecessor_cursor(self):
        run = self._plan()
        self._execute(
            run,
            0,
            self._page(run, next_cursor="poisoned-predecessor", has_more=True),
        )
        current_job_uuid = run.queue_job_uuid
        self.service._finish_failure(
            run,
            current_job_uuid,
            classification="permanent",
            summary="Synthetic terminal predecessor.",
        )
        self.assertEqual(run.state, "failed")

        replacement = self.service._plan_sweep(
            self.source,
            self.connection,
            trigger_kind="retry",
            trigger_ref="restart:%s" % uuid.uuid4(),
        )
        sequence = self.service._restart_catalog_cursor(replacement)
        self.service._enqueue_page(replacement, sequence)
        result, adapter_class = self._execute(
            replacement,
            sequence,
            self._page(replacement),
        )

        call = adapter_class.return_value.fetch_catalog_page.call_args
        self.assertEqual(call.args[1], "campaign")
        self.assertEqual(call.kwargs["after"], "")
        self.assertTrue(result["has_more"])

    def test_daily_catalog_scheduler_is_idempotent(self):
        now = datetime.datetime(2026, 9, 1, 12, 0)
        first = self.service._cron_enqueue_meta_catalog(
            source_ids=[self.source.id],
            now=now,
        )
        second = self.service._cron_enqueue_meta_catalog(
            source_ids=[self.source.id],
            now=now,
        )

        self.assertEqual(first, {"queued": 1, "skipped": 0, "failed": 0})
        self.assertEqual(second, {"queued": 0, "skipped": 1, "failed": 0})
        runs = self.env["marketing.center.sync.run"].search(
            [
                ("source_id", "=", self.source.id),
                ("sync_kind", "=", "catalog"),
                ("trigger_kind", "=", "scheduled"),
            ]
        )
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs.state, "queued")
        self.assertEqual(runs.trigger_ref, "catalog:2026-09-01")

    def test_cursor_cycle_is_bounded_before_a_third_provider_call(self):
        run = self._plan()
        with patch(
            "odoo.addons.marketing_center_meta.models.catalog_sync._MAX_CATALOG_PAGES",
            2,
        ):
            self._execute(
                run,
                0,
                self._page(run, next_cursor="cursor-b", has_more=True),
            )
            self._execute(
                run,
                1,
                self._page(run, next_cursor="cursor-a", has_more=True),
            )
            with patch(_ADAPTER_PATH) as adapter_class:
                result = run.with_context(
                    job_uuid=run.queue_job_uuid
                )._job_sync_meta_catalog_page(2)
        self.assertEqual(result, {"state": "failed"})
        self.assertEqual(run.state, "failed")
        adapter_class.assert_not_called()

    def test_paused_authorization_updates_health_and_stales_run(self):
        run = self._plan()
        binding_revision = self.connection.binding_revision
        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_catalog_page.side_effect = (
                MetaApiPausedError("synthetic authorization revoked")
            )
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_sync_meta_catalog_page(0)
        self.assertEqual(result, {"stale": True, "profile_paused": True})
        self.assertEqual(run.state, "stale")
        self.assertEqual(run.page_count, 0)
        self.assertEqual(self.profile.health_state, "unhealthy")
        self.assertEqual(self.connection.state, "paused")
        self.assertEqual(self.connection.health_state, "unhealthy")
        self.assertGreater(self.connection.binding_revision, binding_revision)
        self.assertFalse(
            self.env["marketing.center.external.entity"].search(
                [("source_id", "=", self.source.id)]
            )
        )

    def test_paused_result_from_old_profile_revision_cannot_poison_health(self):
        run = self._plan()

        def rotate_and_reject(*_args, **_kwargs):
            self.profile.write(
                {"access_token_ref": "ODOO_META_CATALOG_NEW_REVISION_TOKEN"}
            )
            raise MetaApiPausedError("synthetic old-revision rejection")

        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_catalog_page.side_effect = (
                rotate_and_reject
            )
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_sync_meta_catalog_page(0)
        self.assertEqual(result, {"stale": True})
        self.assertEqual(run.state, "stale")
        self.assertEqual(self.profile.health_state, "unknown")
        self.assertFalse(self.profile.last_error_message)

    def test_unexpected_adapter_failure_has_bounded_terminal_policy(self):
        run = self._plan()
        job = (
            self.env["queue.job"]
            .sudo()
            .search([("uuid", "=", run.queue_job_uuid)], limit=1)
        )
        with patch(_ADAPTER_PATH) as adapter_class:
            adapter_class.return_value.fetch_catalog_page.side_effect = RuntimeError(
                "access_token=synthetic-secret-must-not-persist"
            )
            with self.assertRaises(RetryableJobError):
                run.with_context(
                    job_uuid=run.queue_job_uuid
                )._job_sync_meta_catalog_page(0)
            job.sudo().write({"retry": 7})
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_sync_meta_catalog_page(0)
        self.assertEqual(result, {"state": "failed"})
        self.assertEqual(run.state, "failed")
        self.assertNotIn("synthetic-secret", run.error_summary or "")
        self.assertEqual(run.page_count, 0)

    def test_unexpected_successor_enqueue_rolls_back_complete_page(self):
        run = self._plan()
        cursor_before = self.env["marketing.center.sync.cursor"].search(
            [("source_id", "=", self.source.id)], limit=1
        )
        self.assertTrue(cursor_before)
        cursor_snapshot = (
            cursor_before.id,
            cursor_before.cursor_sequence,
            cursor_before.cursor_value,
            cursor_before.cursor_digest,
            cursor_before.last_success_run_id.id,
        )
        job = (
            self.env["queue.job"]
            .sudo()
            .search([("uuid", "=", run.queue_job_uuid)], limit=1)
        )
        job.sudo().write({"retry": 7})
        page = self._page(
            run,
            (self._entity("campaign", "act_123/campaigns/rollback"),),
        )
        with patch(_ADAPTER_PATH) as adapter_class, patch(
            "odoo.addons.marketing_center_meta.models.catalog_sync."
            "MarketingCenterMetaCatalogService._enqueue_page",
            side_effect=RuntimeError("synthetic enqueue failure"),
        ):
            adapter_class.return_value.fetch_catalog_page.return_value = page
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_sync_meta_catalog_page(0)
        self.assertEqual(result, {"state": "failed"})
        self.assertEqual(run.state, "failed")
        self.assertEqual(run.page_count, 0)
        self.assertFalse(
            self.env["marketing.center.external.entity"].search(
                [("external_ref", "=", "act_123/campaigns/rollback")]
            )
        )
        cursor_after = self.env["marketing.center.sync.cursor"].search(
            [("source_id", "=", self.source.id)], limit=1
        )
        self.assertEqual(
            (
                cursor_after.id,
                cursor_after.cursor_sequence,
                cursor_after.cursor_value,
                cursor_after.cursor_digest,
                cursor_after.last_success_run_id.id,
            ),
            cursor_snapshot,
        )

import base64
import datetime
import importlib.util
import uuid
from pathlib import Path
from unittest.mock import patch

from psycopg2 import OperationalError

from odoo.exceptions import AccessError, ValidationError

from odoo.addons.contact_center_base.models.retention import _service_context
from odoo.addons.queue_job.tests.common import trap_jobs

from .test_lifecycle_bridge import MarketingLifecycleCase


class TestMarketingRetentionBridge(MarketingLifecycleCase):
    def setUp(self):
        super().setUp()
        self.now = datetime.datetime(2026, 9, 12, 12)
        self.retention = self.env["contact.center.retention"]
        supported = patch.object(
            type(self.retention),
            "_supported",
            lambda service, binding: binding.conversation_type == "group",
        )
        supported.start()
        self.addCleanup(supported.stop)
        self.account.with_context(**_service_context()).write(
            {"retention_enabled": True, "retention_days": 7}
        )
        self.channel = self.env["mail.channel"]._contact_center_create_channel(
            account=self.account,
            identity=self.env["contact.center.identity"],
            teams=self.team,
            conversation_type="group",
            guest_ids=[],
        )
        self.binding = self.env["contact.center.channel.binding"].create(
            {
                "channel_id": self.channel.id,
                "account_id": self.account.id,
                "conversation_type": "group",
                "conversation_ref": "%s@g.us" % uuid.uuid4().int,
            }
        )

    def _source(self, inbound=True, date="2026-09-01 10:00:00"):
        return self._message_binding(
            self.binding,
            direction="inbound" if inbound else "outbound",
            origin="provider" if inbound else "external_device",
            date=date,
            delivery_state="delivered" if inbound else "sent",
            skip_enqueue=True,
        )

    def _settle(self):
        for kind in ("conversation_started", "first_human_response"):
            self.service._sync_event(self.binding, kind)
        for _page in range(20):
            if not self.episode_service._reconcile_channel(self.binding)["has_more"]:
                return
        self.fail("The bounded fixture timeline did not drain")

    def _purge(self, **kwargs):
        return self.retention._purge_binding(self.binding, now=self.now, **kwargs)

    def _facts(self):
        events = self.env["marketing.business.event"].search(
            [
                ("source_system", "=", "contact_center"),
                ("source_model", "=", "mail.channel"),
                ("source_res_id", "=", self.channel.id),
            ],
            order="id",
        )
        return events.read(["business_event_key", "occurred_at", "snapshot_json"])

    def test_purge_removes_content_and_media_preserving_all_four_ledgers(self):
        inbound = self._source()
        response = self._source(False, "2026-09-01 10:02:00")
        originals = inbound | response
        messages = originals.message_id
        attachment = (
            self.env["ir.attachment"]
            .with_context(**_service_context())
            .create(
                {
                    "name": "retention-fixture.png",
                    "datas": base64.b64encode(b"private original media"),
                    "res_model": "mail.message",
                    "res_id": messages[0].id,
                }
            )
        )
        messages[0].with_context(**_service_context()).write(
            {"attachment_ids": [(4, attachment.id)]}
        )
        self._settle()
        facts = self._facts()
        episode = self.env["marketing.contact.center.response.episode"].search(
            [("channel_binding_id", "=", self.binding.id)]
        )
        answer = episode.response_id
        signals = self.env["marketing.contact.center.response.signal"].search(
            [("channel_binding_id", "=", self.binding.id)]
        )
        cursor = self.episode_service._cursor(self.binding)
        cursor_before = (
            cursor.last_message_res_id,
            cursor.last_signal_id.id,
            cursor.last_sequence,
        )
        identities = (episode.public_ref, answer.public_ref, answer.response_key)
        result = self._purge()
        self.assertEqual(result["message_count"], 2)
        self.assertFalse(messages.exists())
        self.assertFalse(originals.exists())
        self.assertFalse(attachment.exists())
        self.assertTrue(self.channel.exists())
        self.assertEqual(len(signals.exists()), 2)
        self.assertFalse(signals.message_binding_id)
        self.assertFalse(episode.start_message_binding_id)
        self.assertFalse(answer.message_binding_id)
        self.assertTrue(all(signals.mapped("source_message_ref")))
        self.assertEqual(
            (episode.public_ref, answer.public_ref, answer.response_key), identities
        )
        self.assertEqual(
            (
                cursor.last_message_res_id,
                cursor.last_signal_id.id,
                cursor.last_sequence,
            ),
            cursor_before,
        )
        self.assertFalse(cursor.last_message_binding_id)
        self.assertEqual(self._facts(), facts)
        self.assertEqual(self._purge()["message_count"], 0)
        self._settle()
        self.assertEqual(self._facts(), facts)

    def test_expired_open_episode_accepts_a_new_response_and_advances_cursor(self):
        old = self._source()
        self._settle()
        episode = self.env["marketing.contact.center.response.episode"].search(
            [("channel_binding_id", "=", self.binding.id)]
        )
        old_id = old.id
        self.assertEqual(self._purge()["message_count"], 1)
        self.assertEqual(episode.source_message_res_id, old_id)
        self.assertFalse(episode.start_message_binding_id)
        response = self._source(False, "2026-09-12 11:00:00")
        with trap_jobs():
            self.episode_service._record_signals(response)
        self._settle()
        self.assertEqual(episode.response_id.message_binding_id, response)
        self.assertEqual(
            episode.response_id.responded_at, datetime.datetime(2026, 9, 12, 11)
        )
        cursor = self.episode_service._cursor(self.binding)
        self.assertEqual(cursor.last_message_res_id, response.id)
        self.assertFalse(cursor.pending_episode_id)
        self.assertEqual(len(self._events(self.channel, "interaction_started")), 1)
        facts = self._facts()
        self._settle()
        self.assertEqual(self._facts(), facts)

    def test_late_timeline_replay_uses_retained_signal_facts(self):
        self._source()
        self._source(False, "2026-09-01 10:02:00")
        self._settle()
        self._purge()
        facts = self._facts()
        cursor = self.episode_service._cursor(self.binding)
        self.episode_service._write_cursor(
            cursor,
            {
                "last_signal_id": False,
                "last_observed_at": False,
                "last_message_binding_id": False,
                "last_sequence": 0,
                "pending_episode_id": False,
            },
        )
        self._settle()
        self.assertEqual(self._facts(), facts)
        self.assertEqual(cursor.last_sequence, 1)

    def test_retention_preparation_is_bounded_and_preserves_sources_until_ready(self):
        self._source()
        self._source(False, "2026-09-01 10:02:00")
        self._source(True, "2026-09-01 10:03:00")
        original_page_size = type(self.episode_service)._page_size
        with patch.object(
            type(self.episode_service),
            "_page_size",
            lambda service, size=None: original_page_size(service, 1),
        ):
            first = self._purge()
            self.assertTrue(first["staging"])
            self.assertFalse(self.binding.retention_expired_before)
            self.assertEqual(
                self.env["contact.center.message.binding"].search_count(
                    [("channel_binding_id", "=", self.binding.id)]
                ),
                3,
            )
            removed = 0
            for _page in range(12):
                removed += self._purge()["message_count"]
                if removed:
                    break
        self.assertEqual(removed, 3)
        self.assertEqual(
            self.env["marketing.contact.center.response.signal"].search_count(
                [("channel_binding_id", "=", self.binding.id)]
            ),
            3,
        )

    def test_agents_cannot_detach_or_rewrite_retained_evidence(self):
        self._source()
        self._settle()
        signal = self.env["marketing.contact.center.response.signal"].search(
            [("channel_binding_id", "=", self.binding.id)]
        )
        with self.assertRaises(AccessError):
            signal.with_user(self.agent).write({"message_binding_id": False})
        with self.assertRaises(AccessError):
            signal.with_context(marketing_contact_center_retention_token=True).write(
                {"source_expired_at": self.now}
            )

    def test_unrecognized_business_reference_still_rolls_back_retention(self):
        source = self._source()
        self._settle()
        facts = self._facts()
        with self.assertRaises(ValidationError), self.cr.savepoint():
            with patch.object(
                type(self.retention),
                "_retention_guard_optional_consumers",
                side_effect=ValidationError("Business source is protected"),
            ):
                self._purge()
        self.assertTrue(source.exists())
        self.assertEqual(self._facts(), facts)
        signal = self.env["marketing.contact.center.response.signal"].search(
            [("message_binding_id", "=", source.id)]
        )
        self.assertTrue(signal)
        self.assertFalse(signal.source_expired_at)

    def test_busy_projection_lock_preserves_content_and_metrics(self):
        source = self._source()
        self._settle()
        facts = self._facts()
        with patch.object(
            type(self.episode_service),
            "_lock_channel",
            side_effect=OperationalError("Concurrent projection"),
        ), self.assertRaises(OperationalError):
            self._purge()
        self.assertTrue(source.exists())
        self.assertEqual(self._facts(), facts)
        self.assertFalse(self.binding.retention_expired_before)

    def test_confirmed_delivery_source_can_expire_without_changing_response_time(self):
        self._source()
        answer = self._message_binding(
            self.binding,
            direction="outbound",
            origin="agent",
            date="2026-09-01 10:02:00",
            delivery_state="sent",
            skip_enqueue=True,
        )
        delivered_at = datetime.datetime(2026, 9, 1, 10, 2, 5)
        delivery = self.env["contact.center.delivery.event"].create(
            {
                "message_binding_id": answer.id,
                "state": "sent",
                "occurred_at": delivered_at,
            }
        )
        self._settle()
        response = self.env["marketing.contact.center.response"].search(
            [("message_binding_id", "=", answer.id)]
        )
        self.assertEqual(response.delivery_event_id, delivery)
        original_delivery_id = delivery.id
        facts = self._facts()
        self._purge()
        self.assertFalse(delivery.exists())
        self.assertFalse(response.delivery_event_id)
        self.assertEqual(response.source_delivery_res_id, original_delivery_id)
        self.assertEqual(response.responded_at, delivered_at)
        self.assertEqual(self._facts(), facts)

    def test_integer_migration_is_idempotent_and_preserves_business_facts(self):
        self._source()
        self._source(False, "2026-09-01 10:02:00")
        self._settle()
        facts = self._facts()
        # Simulate the pre-upgrade rows: source foreign keys exist, new numeric
        # columns are NULL. The migration must only copy those original IDs.
        self.env.flush_all()
        self.cr.execute(
            "UPDATE marketing_contact_center_response_signal "
            "SET source_message_res_id = NULL WHERE channel_binding_id = %s",
            [self.binding.id],
        )
        self.cr.execute(
            "UPDATE marketing_contact_center_response_cursor "
            "SET last_message_res_id = NULL, backfill_cutoff_message_res_id = NULL, "
            "backfill_after_message_res_id = NULL WHERE channel_binding_id = %s",
            [self.binding.id],
        )
        path = (
            Path(__file__).resolve().parents[1]
            / "migrations/16.0.1.1.0/pre-migration.py"
        )
        spec = importlib.util.spec_from_file_location("retention_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.cr, "16.0.1.0.1")
        migration.migrate(self.cr, "16.0.1.0.2")
        self.env.invalidate_all()
        signals = self.env["marketing.contact.center.response.signal"].search(
            [("channel_binding_id", "=", self.binding.id)]
        )
        self.assertTrue(
            all(
                row.source_message_res_id == row.message_binding_id.id
                for row in signals
            )
        )
        cursor = self.episode_service._cursor(self.binding)
        self.assertEqual(cursor.last_message_res_id, cursor.last_message_binding_id.id)
        self.assertEqual(self._facts(), facts)
        self.assertEqual(self._purge()["message_count"], 2)

    def test_recent_tail_does_not_starve_a_completed_expiry_batch(self):
        source = self._source()
        self._settle()
        recent = self._source(False, "2026-09-12 11:00:00")
        self.episode_service._record_signals(recent)
        original_size = type(self.episode_service)._page_size
        # A full one-item page reports more work. The old deletion target is
        # already processed, so an active recent tail must not hold it forever.
        with patch.object(
            type(self.episode_service),
            "_page_size",
            lambda service, size=None: original_size(service, 1),
        ):
            result = self._purge()
        self.assertEqual(result["message_count"], 1)
        self.assertFalse(source.exists())
        self.assertTrue(recent.exists())

    def test_company_a_cron_prepares_company_b_group_inside_its_own_scope(self):
        self._source()
        self._settle()
        original_facts = self._facts()
        company_a = self.env.company
        company_b = self.env["res.company"].create({"name": "Retention second company"})
        scoped = (
            self.env["res.company"]
            .sudo()
            .with_context(allowed_company_ids=[company_b.id])
            .with_company(company_b)
            .env
        )
        account = scoped["contact.center.account"].create(
            {
                "name": "Retention other company inbox",
                "company_id": company_b.id,
                "platform": "whatsapp",
            }
        )
        account.with_context(**_service_context()).write(
            {
                "retention_enabled": True,
                "retention_days": 7,
            }
        )
        channel = scoped["mail.channel"]._contact_center_create_channel(
            account=account,
            identity=scoped["contact.center.identity"],
            conversation_type="group",
            partner_ids=[],
            guest_ids=[],
        )
        binding = scoped["contact.center.channel.binding"].create(
            {
                "channel_id": channel.id,
                "account_id": account.id,
                "conversation_type": "group",
                "conversation_ref": "%s@g.us" % uuid.uuid4().int,
            }
        )
        source = self._message_binding(
            binding,
            direction="inbound",
            origin="provider",
            date="2026-09-01 10:00:00",
            delivery_state="delivered",
            skip_enqueue=True,
        )
        cron = (
            self.retention.sudo()
            .with_context(allowed_company_ids=[company_a.id])
            .with_company(company_a)
        )
        result = cron._purge_binding(binding, now=self.now)
        self.assertEqual(result["message_count"], 1)
        self.assertFalse(source.exists())
        signals = scoped["marketing.contact.center.response.signal"].search(
            [("channel_binding_id", "=", binding.id)]
        )
        self.assertTrue(signals)
        self.assertEqual(signals.company_id, company_b)
        self.assertFalse(signals.message_binding_id)
        self.assertEqual(self._facts(), original_facts)

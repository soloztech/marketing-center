import datetime
import uuid

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.queue_job.tests.common import trap_jobs


class TestMarketingContactCenterLifecycleBridge(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suffix = str(uuid.uuid4())
        contact_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.agent = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Marketing lifecycle agent",
                    "login": "marketing-lifecycle-%s" % suffix,
                    "email": "marketing-lifecycle@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, contact_group.ids)],
                }
            )
        )
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Marketing lifecycle team",
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Marketing lifecycle account",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "marketing-lifecycle-account-%s" % suffix,
                "default_team_id": cls.team.id,
            }
        )
        cls.service = cls.env["marketing.contact.center.lifecycle.service"]
        cls.episode_service = cls.env[
            "marketing.contact.center.response.episode.service"
        ]

    def _conversation(self, *, env=None, account=None, team=None, label=None):
        env = env or self.env
        account = account or self.account
        team = team or self.team
        label = label or "Lifecycle %s" % uuid.uuid4()
        guest = env["mail.guest"].sudo().create({"name": label})
        identity = (
            env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": label,
                    "company_id": account.company_id.id,
                    "mail_guest_id": guest.id,
                }
            )
        )
        channel = env["mail.channel"]._contact_center_create_channel(
            account=account,
            identity=identity,
            team=team,
            guest_ids=guest.ids,
        )
        binding = (
            env["contact.center.channel.binding"]
            .sudo()
            .create(
                {
                    "channel_id": channel.id,
                    "account_id": account.id,
                    "identity_id": identity.id,
                    "conversation_type": "direct",
                    "conversation_ref": "lifecycle-conversation-%s" % uuid.uuid4(),
                }
            )
        )
        return channel, binding

    def _message_binding(
        self,
        channel_binding,
        *,
        direction,
        origin,
        date,
        delivery_state,
        skip_enqueue=False,
    ):
        channel = channel_binding.channel_id
        message = channel.sudo()._contact_center_post(
            origin=(
                "inbound"
                if direction == "inbound"
                else "external_device"
                if origin == "external_device"
                else "outbound"
            ),
            body="Lifecycle fixture",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            date=fields.Datetime.to_datetime(date),
            partner_ids=[],
        )
        model = channel_binding.env["contact.center.message.binding"].sudo()
        if skip_enqueue:
            model = model.with_context(
                marketing_contact_center_skip_lifecycle_enqueue=True
            )
        binding = model.create(
            {
                "message_id": message.id,
                "channel_binding_id": channel_binding.id,
                "direction": direction,
                "origin": origin,
                "content_type": "text",
                "client_message_id": "lifecycle-client-%s" % uuid.uuid4(),
                "external_message_id": "lifecycle-external-%s" % uuid.uuid4(),
                "delivery_state": delivery_state,
            }
        )
        # ``skip_enqueue`` is fixture setup only.  Do not leak that context into
        # later calls made on the returned record: a delivery confirmation is a
        # new production-like operation and must exercise the live signal hook.
        return binding.with_context(
            marketing_contact_center_skip_lifecycle_enqueue=False
        )

    def _events(self, channel, event_type):
        return (
            self.env["marketing.business.event"]
            .sudo()
            .search(
                [
                    ("company_id", "=", channel.contact_center_company_id.id),
                    ("source_system", "=", "contact_center"),
                    ("source_model", "=", "mail.channel"),
                    ("source_res_id", "=", channel.id),
                    ("event_type", "=", event_type),
                ]
            )
        )

    def _reconcile_episode_pages(self, channel_binding, page_size=2):
        results = []
        for _page in range(50):
            result = self.episode_service._reconcile_channel(
                channel_binding,
                page_size=page_size,
            )
            results.append(result)
            if not result["has_more"]:
                return results
        self.fail("Response episode reconciliation did not reach its durable frontier")

    def test_external_projection_enqueues_and_replay_is_idempotent(self):
        channel, channel_binding = self._conversation()
        with trap_jobs() as trap:
            source = self._message_binding(
                channel_binding,
                direction="inbound",
                origin="provider",
                date="2026-09-01 12:00:00",
                delivery_state="delivered",
            )
            trap.assert_jobs_count(2)
            trap.assert_enqueued_job(
                channel_binding._job_sync_marketing_lifecycle,
                args=("conversation_started",),
                properties={
                    "identity_key": channel_binding._marketing_lifecycle_identity_key(
                        "conversation_started"
                    ),
                    "max_retries": 0,
                    "priority": 42,
                },
            )
            trap.assert_enqueued_job(
                source._job_sync_marketing_response_episode,
                properties={
                    "identity_key": (
                        "marketing_contact_center:response_episode:message:%s"
                        % source.id
                    ),
                    "max_retries": 0,
                    "priority": 41,
                },
            )

        first = self.service._sync_event(channel_binding, "conversation_started")
        replay = self.service._sync_event(channel_binding, "conversation_started")
        self.assertEqual(first, replay)
        self.assertEqual(len(self._events(channel, "conversation_started")), 1)
        self.assertEqual(
            first.business_event_key,
            "contact.center:%s:started" % channel.uuid,
        )
        self.assertIn(
            self.service._message_public_ref(source), first.source_occurrence_ref
        )
        self.assertEqual(first.company_id, self.env.company)
        self.assertEqual(first.evidence_level, "provider_asserted")

    def test_lifecycle_wakeups_are_transaction_scoped(self):
        _channel, channel_binding = self._conversation()
        first = channel_binding._marketing_lifecycle_identity_key(
            "conversation_started"
        )
        second = channel_binding._marketing_lifecycle_identity_key(
            "conversation_started"
        )
        backfill = channel_binding._marketing_lifecycle_identity_key(
            "conversation_started", wake_scope="backfill"
        )
        self.assertEqual(first, second)
        self.assertIn(":tx:", first)
        self.assertNotEqual(first, backfill)
        self.assertTrue(backfill.endswith(":backfill"))
        with self.assertRaises(ValidationError):
            channel_binding._marketing_lifecycle_identity_key(
                "conversation_started", wake_scope="invalid"
            )

    def test_first_confirmed_agent_response_has_stable_message_occurrence(self):
        channel, channel_binding = self._conversation()
        self._message_binding(
            channel_binding,
            direction="inbound",
            origin="provider",
            date="2026-09-01 12:00:00",
            delivery_state="delivered",
            skip_enqueue=True,
        )
        self.service._sync_event(channel_binding, "conversation_started")
        response = self._message_binding(
            channel_binding,
            direction="outbound",
            origin="agent",
            date="2026-09-01 12:01:00",
            delivery_state="queued",
        )
        with trap_jobs() as trap:
            response._contact_center_apply_delivery(
                "sent",
                occurred_at=datetime.datetime(2026, 9, 1, 12, 1, 5),
                external_event_id="lifecycle-delivery-%s" % uuid.uuid4(),
            )
            trap.assert_jobs_count(2)
            trap.assert_enqueued_job(
                channel_binding._job_sync_marketing_lifecycle,
                args=("first_human_response",),
                properties={
                    "identity_key": channel_binding._marketing_lifecycle_identity_key(
                        "first_human_response"
                    ),
                    "max_retries": 0,
                    "priority": 42,
                },
            )
            trap.assert_enqueued_job(
                response._job_sync_marketing_response_episode,
                properties={
                    "identity_key": (
                        "marketing_contact_center:response_episode:message:%s"
                        % response.id
                    ),
                    "max_retries": 0,
                    "priority": 41,
                },
            )

        first = self.service._sync_event(channel_binding, "first_human_response")
        replay = self.service._sync_event(channel_binding, "first_human_response")
        message_ref = self.service._message_public_ref(response)
        expected_key = "contact.center:%s:first_response:%s" % (
            channel.uuid,
            message_ref,
        )
        self.assertEqual(first, replay)
        self.assertEqual(first.business_event_key, expected_key)
        self.assertEqual(first.source_occurrence_ref, expected_key)
        self.assertEqual(first.occurred_at, datetime.datetime(2026, 9, 1, 12, 1, 5))
        self.assertEqual(len(self._events(channel, "first_human_response")), 1)

        later = self._message_binding(
            channel_binding,
            direction="outbound",
            origin="agent",
            date="2026-09-01 12:02:00",
            delivery_state="sent",
            skip_enqueue=True,
        )
        self.env["contact.center.delivery.event"].sudo().create(
            {
                "message_binding_id": later.id,
                "state": "sent",
                "occurred_at": datetime.datetime(2026, 9, 1, 12, 2, 5),
            }
        )
        self.assertEqual(
            self.service._sync_event(channel_binding, "first_human_response"),
            first,
        )
        self.assertEqual(len(self._events(channel, "first_human_response")), 1)

    def test_response_requires_external_start_and_excludes_automation(self):
        channel, channel_binding = self._conversation()
        agent = self._message_binding(
            channel_binding,
            direction="outbound",
            origin="agent",
            date="2026-09-01 11:59:00",
            delivery_state="sent",
            skip_enqueue=True,
        )
        self.env["contact.center.delivery.event"].sudo().create(
            {
                "message_binding_id": agent.id,
                "state": "sent",
                "occurred_at": datetime.datetime(2026, 9, 1, 11, 59),
            }
        )
        self.assertFalse(
            self.service._sync_event(channel_binding, "first_human_response")
        )

        self._message_binding(
            channel_binding,
            direction="inbound",
            origin="provider",
            date="2026-09-01 12:00:00",
            delivery_state="delivered",
            skip_enqueue=True,
        )
        automation = self._message_binding(
            channel_binding,
            direction="outbound",
            origin="automation",
            date="2026-09-01 12:01:00",
            delivery_state="sent",
            skip_enqueue=True,
        )
        self.env["contact.center.delivery.event"].sudo().create(
            {
                "message_binding_id": automation.id,
                "state": "sent",
                "occurred_at": datetime.datetime(2026, 9, 1, 12, 1),
            }
        )
        self.assertFalse(
            self.service._sync_event(channel_binding, "first_human_response")
        )
        self.assertFalse(self._events(channel, "first_human_response"))

    def test_company_scope_and_business_event_acl_are_preserved(self):
        other_company = self.env["res.company"].create(
            {"name": "Lifecycle company %s" % uuid.uuid4()}
        )
        allowed_company_ids = [self.env.company.id, other_company.id]
        scoped = (
            self.env["res.company"]
            .sudo()
            .with_context(allowed_company_ids=allowed_company_ids)
            .with_company(other_company)
            .env
        )
        other_agent = (
            scoped["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Other company lifecycle agent",
                    "login": "other-lifecycle-%s" % uuid.uuid4(),
                    "email": "other-lifecycle@example.invalid",
                    "company_id": other_company.id,
                    "company_ids": [(6, 0, allowed_company_ids)],
                    "groups_id": [
                        (
                            6,
                            0,
                            scoped.ref(
                                "contact_center_base.group_contact_center_agent"
                            ).ids,
                        )
                    ],
                }
            )
        )
        team = scoped["contact.center.team"].create(
            {
                "name": "Other lifecycle team",
                "company_id": other_company.id,
                "agent_ids": [(6, 0, other_agent.ids)],
            }
        )
        account = scoped["contact.center.account"].create(
            {
                "name": "Other lifecycle account",
                "company_id": other_company.id,
                "platform": "whatsapp",
                "external_ref": "other-lifecycle-%s" % uuid.uuid4(),
                "default_team_id": team.id,
            }
        )
        channel, channel_binding = self._conversation(
            env=scoped,
            account=account,
            team=team,
            label="Other company lifecycle",
        )
        self._message_binding(
            channel_binding,
            direction="inbound",
            origin="provider",
            date="2026-09-01 12:00:00",
            delivery_state="delivered",
            skip_enqueue=True,
        )
        event = scoped["marketing.contact.center.lifecycle.service"]._sync_event(
            channel_binding, "conversation_started"
        )
        self.assertEqual(event.company_id, other_company)
        self.assertEqual(event.source_res_id, channel.id)

        with self.assertRaises(AccessError):
            event.with_user(self.agent).check_access_rights("read")

    def test_bounded_backfill_enqueues_both_lifecycle_facts(self):
        _channel, channel_binding = self._conversation()
        with self.assertRaises(AccessError):
            self.service._enqueue_backfill(company=self.env["res.company"])
        with trap_jobs() as trap:
            result = self.service._enqueue_backfill(
                company=self.env.company,
                after_id=channel_binding.id - 1,
                limit=1,
            )
            trap.assert_jobs_count(2)
        self.assertEqual(result["enqueued_conversations"], 1)
        self.assertEqual(result["last_id"], channel_binding.id)

    def test_multiple_inbounds_share_one_episode_and_replay_is_idempotent(self):
        channel, channel_binding = self._conversation()
        first_inbound = self._message_binding(
            channel_binding,
            direction="inbound",
            origin="provider",
            date="2026-09-01 13:00:00",
            delivery_state="delivered",
            skip_enqueue=True,
        )
        self._message_binding(
            channel_binding,
            direction="inbound",
            origin="provider",
            date="2026-09-01 13:01:00",
            delivery_state="delivered",
            skip_enqueue=True,
        )
        response = self._message_binding(
            channel_binding,
            direction="outbound",
            origin="agent",
            date="2026-09-01 13:02:00",
            delivery_state="sent",
            skip_enqueue=True,
        )

        first = self.episode_service._reconcile_channel(channel_binding)
        replay = self.episode_service._reconcile_channel(channel_binding)
        episodes = self.env["marketing.contact.center.response.episode"].search(
            [("channel_binding_id", "=", channel_binding.id)]
        )
        responses = self.env["marketing.contact.center.response"].search(
            [("episode_id", "in", episodes.ids)]
        )

        self.assertEqual(first, replay)
        self.assertEqual(first["episode_count"], 1)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(len(responses), 1)
        self.assertEqual(episodes.start_message_binding_id, first_inbound)
        self.assertEqual(responses.message_binding_id, response)
        self.assertEqual(responses.response_origin, "agent")
        self.assertEqual(len(self._events(channel, "interaction_started")), 1)
        self.assertEqual(len(self._events(channel, "first_human_response")), 1)

    def test_external_device_closes_episode_and_next_inbound_opens_another(self):
        channel, channel_binding = self._conversation()
        self._message_binding(
            channel_binding,
            direction="inbound",
            origin="provider",
            date="2026-09-01 14:00:00",
            delivery_state="delivered",
            skip_enqueue=True,
        )
        external = self._message_binding(
            channel_binding,
            direction="outbound",
            origin="external_device",
            date="2026-09-01 14:01:00",
            delivery_state="sent",
            skip_enqueue=True,
        )
        second_inbound = self._message_binding(
            channel_binding,
            direction="inbound",
            origin="provider",
            date="2026-09-01 14:02:00",
            delivery_state="delivered",
            skip_enqueue=True,
        )

        result = self.episode_service._reconcile_channel(channel_binding)
        episodes = self.env["marketing.contact.center.response.episode"].search(
            [("channel_binding_id", "=", channel_binding.id)], order="sequence"
        )

        self.assertEqual(result["episode_count"], 2)
        self.assertEqual(result["pending_episode_ref"], episodes[1].public_ref)
        self.assertEqual(len(episodes), 2)
        self.assertEqual(episodes[0].response_id.message_binding_id, external)
        self.assertEqual(episodes[0].response_id.response_origin, "external_device")
        self.assertEqual(episodes[1].start_message_binding_id, second_inbound)
        self.assertFalse(episodes[1].response_id)
        self.assertEqual(len(self._events(channel, "interaction_started")), 2)
        self.assertEqual(len(self._events(channel, "first_human_response")), 1)

    def test_cold_outbound_and_automation_do_not_close_an_episode(self):
        _channel, channel_binding = self._conversation()
        self._message_binding(
            channel_binding,
            direction="outbound",
            origin="agent",
            date="2026-09-01 14:59:00",
            delivery_state="sent",
            skip_enqueue=True,
        )
        self._message_binding(
            channel_binding,
            direction="inbound",
            origin="provider",
            date="2026-09-01 15:00:00",
            delivery_state="delivered",
            skip_enqueue=True,
        )
        self._message_binding(
            channel_binding,
            direction="outbound",
            origin="automation",
            date="2026-09-01 15:01:00",
            delivery_state="sent",
            skip_enqueue=True,
        )

        result = self.episode_service._reconcile_channel(channel_binding)
        episode = self.env["marketing.contact.center.response.episode"].search(
            [("channel_binding_id", "=", channel_binding.id)]
        )
        self.assertEqual(result["episode_count"], 1)
        self.assertEqual(result["pending_episode_ref"], episode.public_ref)
        self.assertFalse(episode.response_id)

    def test_episode_adopts_matching_conversation_level_response_event(self):
        channel, channel_binding = self._conversation()
        self._message_binding(
            channel_binding,
            direction="inbound",
            origin="provider",
            date="2026-09-01 16:00:00",
            delivery_state="delivered",
            skip_enqueue=True,
        )
        response = self._message_binding(
            channel_binding,
            direction="outbound",
            origin="agent",
            date="2026-09-01 16:01:00",
            delivery_state="sent",
            skip_enqueue=True,
        )
        conversation_event = self.service._sync_event(
            channel_binding, "first_human_response"
        )

        self.episode_service._reconcile_channel(channel_binding)
        projected = self.env["marketing.contact.center.response"].search(
            [("message_binding_id", "=", response.id)]
        )

        self.assertEqual(projected.response_event_id, conversation_event)
        self.assertEqual(len(self._events(channel, "first_human_response")), 1)

    def test_response_episode_ledgers_are_immutable(self):
        _channel, channel_binding = self._conversation()
        self._message_binding(
            channel_binding,
            direction="inbound",
            origin="provider",
            date="2026-09-01 17:00:00",
            delivery_state="delivered",
            skip_enqueue=True,
        )
        self._message_binding(
            channel_binding,
            direction="outbound",
            origin="agent",
            date="2026-09-01 17:01:00",
            delivery_state="sent",
            skip_enqueue=True,
        )
        self.episode_service._reconcile_channel(channel_binding)
        episode = self.env["marketing.contact.center.response.episode"].search(
            [("channel_binding_id", "=", channel_binding.id)]
        )

        with self.assertRaises(AccessError):
            episode.write({"sequence": 2})
        with self.assertRaises(AccessError):
            episode.response_id.unlink()

    def test_late_confirmation_cannot_close_or_reorder_newer_episode(self):
        _channel, channel_binding = self._conversation()
        late = self._message_binding(
            channel_binding,
            direction="outbound",
            origin="agent",
            date="2026-09-01 17:00:00",
            delivery_state="queued",
            skip_enqueue=True,
        )
        inbound = self._message_binding(
            channel_binding,
            direction="inbound",
            origin="provider",
            date="2026-09-01 17:05:00",
            delivery_state="delivered",
            skip_enqueue=True,
        )
        first = self.episode_service._reconcile_channel(channel_binding)
        cursor = self.env["marketing.contact.center.response.cursor"].search(
            [("channel_binding_id", "=", channel_binding.id)]
        )
        consumed_at = cursor.last_observed_at

        late._contact_center_apply_delivery(
            "sent",
            occurred_at=datetime.datetime(2026, 9, 1, 17, 1, 0),
            external_event_id="late-confirmation-%s" % uuid.uuid4(),
        )
        second = self.episode_service._reconcile_channel(channel_binding)
        episode = self.env["marketing.contact.center.response.episode"].search(
            [("channel_binding_id", "=", channel_binding.id)]
        )

        self.assertEqual(first["episode_count"], 1)
        self.assertEqual(second["episode_count"], 1)
        self.assertEqual(episode.start_message_binding_id, inbound)
        self.assertFalse(episode.response_id)
        late_signal = self.env["marketing.contact.center.response.signal"].search(
            [("message_binding_id", "=", late.id)]
        )
        cursor.invalidate_cache()
        self.assertTrue(late_signal)
        self.assertGreater(late_signal.observed_at, consumed_at)
        self.assertEqual(cursor.last_signal_id, late_signal)

        # After the initial snapshot, runtime messages are inserted through the
        # normal signal hook; a completed cursor deliberately does not rescan
        # the whole source conversation.
        with trap_jobs():
            valid = self._message_binding(
                channel_binding,
                direction="outbound",
                origin="external_device",
                date="2026-09-01 17:06:00",
                delivery_state="sent",
            )
        final = self.episode_service._reconcile_channel(channel_binding)
        self.assertFalse(final["pending_episode_ref"])
        self.assertEqual(episode.response_id.message_binding_id, valid)

    def test_old_authored_message_confirmed_late_does_not_close_next_episode(self):
        _channel, channel_binding = self._conversation()
        self._message_binding(
            channel_binding,
            direction="inbound",
            origin="provider",
            date="2026-09-01 18:00:00",
            delivery_state="delivered",
            skip_enqueue=True,
        )
        stale = self._message_binding(
            channel_binding,
            direction="outbound",
            origin="agent",
            date="2026-09-01 18:01:00",
            delivery_state="queued",
            skip_enqueue=True,
        )
        external = self._message_binding(
            channel_binding,
            direction="outbound",
            origin="external_device",
            date="2026-09-01 18:02:00",
            delivery_state="sent",
            skip_enqueue=True,
        )
        second_inbound = self._message_binding(
            channel_binding,
            direction="inbound",
            origin="provider",
            date="2026-09-01 18:03:00",
            delivery_state="delivered",
            skip_enqueue=True,
        )
        self.episode_service._reconcile_channel(channel_binding)

        episodes = self.env["marketing.contact.center.response.episode"].search(
            [("channel_binding_id", "=", channel_binding.id)], order="sequence"
        )
        self.assertEqual(len(episodes), 2)
        self.assertEqual(episodes[0].response_id.message_binding_id, external)
        self.assertEqual(episodes[1].start_message_binding_id, second_inbound)
        self.assertFalse(episodes[1].response_id)

        stale._contact_center_apply_delivery(
            "sent",
            occurred_at=datetime.datetime(2026, 9, 1, 18, 4, 0),
            external_event_id="stale-late-confirmation-%s" % uuid.uuid4(),
        )
        result = self.episode_service._reconcile_channel(channel_binding)

        self.assertEqual(result["episode_count"], 2)
        self.assertEqual(result["pending_episode_ref"], episodes[1].public_ref)
        self.assertFalse(episodes[1].response_id)

    def test_external_device_delivery_is_not_response_evidence(self):
        _channel, channel_binding = self._conversation()
        self._message_binding(
            channel_binding,
            direction="inbound",
            origin="provider",
            date="2026-09-01 19:00:00",
            delivery_state="delivered",
            skip_enqueue=True,
        )
        external = self._message_binding(
            channel_binding,
            direction="outbound",
            origin="external_device",
            date="2026-09-01 19:01:00",
            delivery_state="queued",
            skip_enqueue=True,
        )
        external.with_context(
            marketing_contact_center_skip_lifecycle_enqueue=True
        )._contact_center_apply_delivery(
            "delivered",
            occurred_at=datetime.datetime(2026, 9, 1, 19, 1, 5),
            external_event_id="external-device-delivery-%s" % uuid.uuid4(),
        )
        delivery = self.env["contact.center.delivery.event"].search(
            [("message_binding_id", "=", external.id)]
        )
        self.assertTrue(delivery)

        result = self.episode_service._reconcile_channel(channel_binding)

        signal = self.env["marketing.contact.center.response.signal"].search(
            [("message_binding_id", "=", external.id)]
        )
        response = self.env["marketing.contact.center.response"].search(
            [("message_binding_id", "=", external.id)]
        )
        self.assertFalse(result["pending_episode_ref"])
        self.assertEqual(signal.response_origin, "external_device")
        self.assertFalse(signal.delivery_event_id)
        self.assertEqual(response.response_origin, "external_device")
        self.assertFalse(response.delivery_event_id)

    def test_response_history_uses_multiple_bounded_pages(self):
        _channel, channel_binding = self._conversation()
        starts = []
        for offset in range(3):
            starts.append(
                self._message_binding(
                    channel_binding,
                    direction="inbound",
                    origin="provider",
                    date="2026-09-01 20:0%s:00" % (offset * 2),
                    delivery_state="delivered",
                    skip_enqueue=True,
                )
            )
            self._message_binding(
                channel_binding,
                direction="outbound",
                origin="agent",
                date="2026-09-01 20:0%s:00" % (offset * 2 + 1),
                delivery_state="sent",
                skip_enqueue=True,
            )
        starts.append(
            self._message_binding(
                channel_binding,
                direction="inbound",
                origin="provider",
                date="2026-09-01 20:06:00",
                delivery_state="delivered",
                skip_enqueue=True,
            )
        )

        results = self._reconcile_episode_pages(channel_binding, page_size=2)
        episodes = self.env["marketing.contact.center.response.episode"].search(
            [("channel_binding_id", "=", channel_binding.id)], order="sequence"
        )
        cursor = self.env["marketing.contact.center.response.cursor"].search(
            [("channel_binding_id", "=", channel_binding.id)]
        )

        self.assertGreater(len(results), 4)
        self.assertEqual(
            episodes.mapped("start_message_binding_id").ids,
            [message.id for message in starts],
        )
        self.assertEqual(len(episodes.mapped("response_id")), 3)
        self.assertEqual(cursor.backfill_state, "done")
        self.assertEqual(cursor.last_sequence, 4)
        self.assertTrue(cursor.last_observed_at)

    def test_live_signal_waits_behind_the_backfill_cutoff(self):
        _channel, channel_binding = self._conversation()
        first = self._message_binding(
            channel_binding,
            direction="inbound",
            origin="provider",
            date="2026-09-01 21:00:00",
            delivery_state="delivered",
            skip_enqueue=True,
        )
        self._message_binding(
            channel_binding,
            direction="outbound",
            origin="agent",
            date="2026-09-01 21:01:00",
            delivery_state="sent",
            skip_enqueue=True,
        )
        second = self._message_binding(
            channel_binding,
            direction="inbound",
            origin="provider",
            date="2026-09-01 21:02:00",
            delivery_state="delivered",
            skip_enqueue=True,
        )
        self._message_binding(
            channel_binding,
            direction="outbound",
            origin="agent",
            date="2026-09-01 21:03:00",
            delivery_state="sent",
            skip_enqueue=True,
        )

        first_page = self.episode_service._reconcile_channel(
            channel_binding, page_size=2
        )
        self.assertEqual(first_page["backfill_state"], "materializing")
        self.assertFalse(
            self.env["marketing.contact.center.response.episode"].search_count(
                [("channel_binding_id", "=", channel_binding.id)]
            )
        )

        with trap_jobs():
            live = self._message_binding(
                channel_binding,
                direction="inbound",
                origin="provider",
                date="2026-09-01 21:04:00",
                delivery_state="delivered",
            )
        second_page = self.episode_service._reconcile_channel(
            channel_binding, page_size=2
        )
        self.assertEqual(second_page["backfill_state"], "materializing")
        self.assertFalse(
            self.env["marketing.contact.center.response.episode"].search_count(
                [("channel_binding_id", "=", channel_binding.id)]
            )
        )

        self._reconcile_episode_pages(channel_binding, page_size=2)
        episodes = self.env["marketing.contact.center.response.episode"].search(
            [("channel_binding_id", "=", channel_binding.id)], order="sequence"
        )
        self.assertEqual(
            episodes.mapped("start_message_binding_id").ids,
            (first | second | live).ids,
        )
        self.assertEqual(episodes[-1].start_message_binding_id, live)

    def test_response_cursor_is_monotonic_and_replay_safe(self):
        Cursor = self.env["marketing.contact.center.response.cursor"]
        self.assertNotIn("legacy_consumed_signal_id", Cursor._fields)
        self.assertEqual(
            [value for value, _label in Cursor._fields["backfill_state"].selection],
            ["pending", "materializing", "processing", "done"],
        )
        _channel, channel_binding = self._conversation()
        self._message_binding(
            channel_binding,
            direction="inbound",
            origin="provider",
            date="2026-09-01 22:00:00",
            delivery_state="delivered",
            skip_enqueue=True,
        )
        self._reconcile_episode_pages(channel_binding, page_size=1)
        cursor = self.env["marketing.contact.center.response.cursor"].search(
            [("channel_binding_id", "=", channel_binding.id)]
        )
        initial_frontier = (
            cursor.last_observed_at,
            cursor.last_message_binding_id.id,
            cursor.last_signal_id.id,
        )
        initial_sequence = cursor.last_sequence

        with trap_jobs():
            response = self._message_binding(
                channel_binding,
                direction="outbound",
                origin="external_device",
                date="2026-09-01 22:01:00",
                delivery_state="sent",
            )
        self._reconcile_episode_pages(channel_binding, page_size=1)
        cursor.invalidate_cache()
        advanced_frontier = (
            cursor.last_observed_at,
            cursor.last_message_binding_id.id,
            cursor.last_signal_id.id,
        )
        response_count = self.env["marketing.contact.center.response"].search_count(
            [("message_binding_id", "=", response.id)]
        )

        self.assertGreater(advanced_frontier, initial_frontier)
        self.assertEqual(cursor.last_sequence, initial_sequence)
        self.assertEqual(response_count, 1)
        replay = self.episode_service._reconcile_channel(channel_binding, page_size=1)
        cursor.invalidate_cache()
        self.assertFalse(replay["has_more"])
        self.assertEqual(
            (
                cursor.last_observed_at,
                cursor.last_message_binding_id.id,
                cursor.last_signal_id.id,
            ),
            advanced_frontier,
        )
        self.assertEqual(
            self.env["marketing.contact.center.response"].search_count(
                [("message_binding_id", "=", response.id)]
            ),
            1,
        )

    def test_response_continuation_has_a_page_specific_identity(self):
        _channel, channel_binding = self._conversation()
        with trap_jobs() as trap:
            channel_binding._enqueue_marketing_response_episode_continuation(
                "durable-frontier"
            )
            trap.assert_jobs_count(1)
            trap.assert_enqueued_job(
                channel_binding._job_sync_marketing_response_episodes,
                properties={
                    "identity_key": (
                        "marketing_contact_center:response_episode:channel:%s:"
                        "page:durable-frontier" % channel_binding.id
                    ),
                    "max_retries": 0,
                    "priority": 43,
                },
            )

    def test_response_episode_backfill_is_bounded(self):
        _channel, channel_binding = self._conversation()
        with self.assertRaises(AccessError):
            self.episode_service._enqueue_backfill(company=self.env["res.company"])
        with trap_jobs() as trap:
            result = self.episode_service._enqueue_backfill(
                company=self.env.company,
                after_id=channel_binding.id - 1,
                limit=1,
            )
            trap.assert_jobs_count(1)
            trap.assert_enqueued_job(
                channel_binding._job_sync_marketing_response_episodes,
                properties={
                    "identity_key": (
                        "marketing_contact_center:response_episode:channel:%s"
                        % channel_binding.id
                    ),
                    "max_retries": 0,
                    "priority": 43,
                },
            )
        self.assertEqual(result["enqueued_conversations"], 1)
        self.assertEqual(result["last_id"], channel_binding.id)

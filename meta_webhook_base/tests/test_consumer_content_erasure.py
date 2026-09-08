from unittest.mock import patch

from odoo.exceptions import AccessError

from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.tokens import META_WEBHOOK_INTERNAL_TOKEN
from .common import MetaWebhookCase


class TestMetaWebhookConsumerContentErasure(MetaWebhookCase):
    CONSUMER = "contact_center.meta"
    OTHER_CONSUMER = "test.independent_consumer"

    def _subscribe(self, consumer=None, field_name="messages"):
        return self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.page.id,
                "consumer_key": consumer or self.CONSUMER,
                "object_type": "page",
                "field_name": field_name,
            }
        )

    def _messaging_delivery(self, count=1, include_leadgen=False):
        envelope = self.messaging_envelope()
        entry = envelope["entry"][0]
        entry["messaging"] = [
            {
                "sender": {"id": str(900 + index)},
                "message": {
                    "mid": "synthetic-message-%s" % index,
                    "text": "Synthetic message %s" % index,
                },
            }
            for index in range(count)
        ]
        if include_leadgen:
            entry["changes"] = self.leadgen_envelope()["entry"][0]["changes"]
        specs = [
            {
                "item_key": "entry:0:messaging:%s" % index,
                "object_type": "page",
                "event_field": "messages",
                "target_asset_id": self.PAGE_ID,
                "occurrence_ref": "synthetic-message-%s" % index,
                "payload_json": {
                    "schema_version": "meta.webhook.v1",
                    "messaging": message,
                },
            }
            for index, message in enumerate(entry["messaging"])
        ]
        # Exercise the real shared item validation/persistence without depending
        # on an optional consumer addon or creating its private artifacts.
        dispatcher_class = type(self.env["meta.webhook.dispatcher"])
        with patch.object(dispatcher_class, "_consumer_item_specs", return_value=specs):
            return self.create_delivery(envelope)

    def _internal(self, records):
        return records.sudo().with_context(
            allowed_company_ids=self.endpoint.company_id.ids,
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN,
        )

    def _fanout(self, delivery):
        with trap_jobs():
            self._internal(delivery)._fanout_once()

    def test_erasure_requires_process_local_capability_even_under_sudo(self):
        self._subscribe()
        item = self._messaging_delivery().item_ids.ensure_one()
        before = item.payload_json
        for supplied in (None, True, "META_WEBHOOK_INTERNAL_TOKEN", object()):
            with self.subTest(token_type=type(supplied).__name__):
                with self.assertRaises(AccessError):
                    item.sudo().with_context(
                        meta_webhook_internal=supplied
                    )._erase_consumer_message_content(self.CONSUMER)
        for consumer in (False, None, "", 7):
            with self.subTest(consumer=consumer), self.assertRaises(AccessError):
                self._internal(item)._erase_consumer_message_content(consumer)
        item.invalidate_recordset(["payload_json"])
        self.assertEqual(item.payload_json, before)
        self.assertEqual(
            self._internal(item)._erase_consumer_message_content(self.CONSUMER), item
        )

    def test_erasure_rejects_another_company_without_mutation(self):
        self._subscribe()
        item = self._messaging_delivery().item_ids.ensure_one()
        other_company = self.env["res.company"].create(
            {"name": "Unrelated erasure company"}
        )
        before = item.payload_json
        wrong_scope = self._internal(item).with_context(
            allowed_company_ids=other_company.ids
        )
        with self.assertRaises(AccessError):
            wrong_scope._erase_consumer_message_content(self.CONSUMER)
        item.invalidate_recordset(["payload_json"])
        self.assertEqual(item.payload_json, before)
        self.assertEqual(
            self._internal(item)._erase_consumer_message_content(self.CONSUMER), item
        )

    def test_nonmessaging_evidence_cannot_be_erased(self):
        self._subscribe(field_name="leadgen")
        leadgen = self.create_delivery(self.leadgen_envelope()).item_ids.ensure_one()
        dispatcher_class = type(self.env["meta.webhook.dispatcher"])
        with patch.object(dispatcher_class, "_consumer_item_specs", return_value=()):
            unknown = self.create_delivery(
                self.messaging_envelope()
            ).item_ids.ensure_one()
        self.assertEqual(leadgen.kind, "leadgen")
        self.assertEqual(unknown.kind, "unknown")
        for item in (leadgen, unknown):
            before = item.payload_json
            with self.subTest(kind=item.kind), self.assertRaises(AccessError):
                self._internal(item)._erase_consumer_message_content(self.CONSUMER)
            item.invalidate_recordset(["payload_json"])
            self.assertEqual(item.payload_json, before)

    def test_erasure_preserves_receipts_hashes_and_other_items_in_delivery(self):
        self._subscribe()
        # Another consumer of an unrelated field does not own these messages.
        self._subscribe(self.OTHER_CONSUMER, field_name="leadgen")
        delivery = self._messaging_delivery(count=2, include_leadgen=True)
        self._fanout(delivery)
        item = delivery.item_ids.filtered(
            lambda row: row.item_key == "entry:0:messaging:0"
        ).ensure_one()
        other_items = delivery.item_ids - item
        self.assertEqual(len(other_items), 2)
        self.assertTrue(delivery.dispatch_ids)
        item_fields = [
            "delivery_id",
            "sequence",
            "item_key",
            "kind",
            "object_type",
            "event_field",
            "target_asset_id",
            "occurrence_ref",
            "event_sha256",
        ]
        delivery_fields = [
            "public_ref",
            "content_sha256",
            "sanitized_envelope_json",
            "body_size_bytes",
            "state",
            "item_count",
            "dispatch_count",
        ]
        dispatch_fields = [
            "item_id",
            "delivery_id",
            "consumer_key",
            "page_id",
            "page_revision",
            "state",
            "queue_job_uuid",
            "result_ref",
        ]
        before_item = item.read(item_fields)
        before_delivery = delivery.read(delivery_fields)
        before_dispatches = delivery.dispatch_ids.read(dispatch_fields)
        before_other_items = other_items.read(item_fields + ["payload_json"])

        self.assertEqual(
            self._internal(item)._erase_consumer_message_content(self.CONSUMER), item
        )

        item.invalidate_recordset(["payload_json"])
        self.assertEqual(item.payload_json["reason"], "consumer_content_erased")
        self.assertEqual(item.payload_json["consumer_key"], self.CONSUMER)
        self.assertNotIn("messaging", item.payload_json)
        self.assertEqual(item.read(item_fields), before_item)
        self.assertEqual(delivery.read(delivery_fields), before_delivery)
        self.assertEqual(delivery.dispatch_ids.read(dispatch_fields), before_dispatches)
        self.assertEqual(
            other_items.read(item_fields + ["payload_json"]), before_other_items
        )

    def test_different_consumer_cannot_erase_exclusive_owner_content(self):
        self._subscribe()
        item = self._messaging_delivery().item_ids.ensure_one()
        before = item.payload_json
        self.assertFalse(
            self._internal(item)._erase_consumer_message_content(self.OTHER_CONSUMER)
        )
        self.assertEqual(item.payload_json, before)

    def test_active_co_consumer_prevents_erasure_before_fanout(self):
        self._subscribe()
        self._subscribe(self.OTHER_CONSUMER)
        item = self._messaging_delivery().item_ids.ensure_one()
        before = item.payload_json
        self.assertFalse(item.dispatch_ids)
        self.assertFalse(
            self._internal(item)._erase_consumer_message_content(self.CONSUMER)
        )
        self.assertEqual(item.payload_json, before)

    def test_historic_dispatch_prevents_erasure_after_co_consumer_is_archived(self):
        self._subscribe()
        other_subscription = self._subscribe(self.OTHER_CONSUMER)
        delivery = self._messaging_delivery()
        self._fanout(delivery)
        item = delivery.item_ids.ensure_one()
        self.assertEqual(
            set(item.dispatch_ids.mapped("consumer_key")),
            {self.CONSUMER, self.OTHER_CONSUMER},
        )
        other_subscription.write({"active": False})
        before = item.payload_json
        self.assertFalse(
            self._internal(item)._erase_consumer_message_content(self.CONSUMER)
        )
        self.assertEqual(item.payload_json, before)

    def test_repeated_erasure_keeps_tombstone_and_terminal_dispatch_receipt(self):
        self._subscribe()
        delivery = self._messaging_delivery()
        self._fanout(delivery)
        item = delivery.item_ids.ensure_one()
        dispatch = item.dispatch_ids.ensure_one()
        self._internal(dispatch).write(
            {"state": "done", "result_ref": "synthetic:durable-consumer-receipt"}
        )
        self._internal(item)._erase_consumer_message_content(self.CONSUMER)
        tombstone = item.payload_json
        digest = item.event_sha256

        self._internal(item)._erase_consumer_message_content(self.CONSUMER)

        self.assertEqual(item.payload_json, tombstone)
        self.assertEqual(item.event_sha256, digest)
        self.assertEqual(dispatch.state, "done")
        self.assertEqual(dispatch.result_ref, "synthetic:durable-consumer-receipt")
        # A replay of an already completed dispatch cannot reach any consumer,
        # so the erased payload cannot recreate a downstream conversation.
        dispatcher_class = type(self.env["meta.webhook.dispatcher"])
        with patch.object(dispatcher_class, "_dispatch_consumer") as consume:
            self.assertFalse(
                self._internal(dispatch)
                .with_context(job_uuid=dispatch.queue_job_uuid)
                ._job_process()
            )
        consume.assert_not_called()

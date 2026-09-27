import json
import os
from unittest.mock import patch

from odoo.exceptions import AccessError, ValidationError

from odoo.addons.meta_api_base.services.credentials import MetaCredentialResolutionError
from odoo.addons.queue_job.tests.common import trap_jobs

from ..models import page as page_module
from ..services.sanitizer import MetaWebhookSanitizationError, sanitized_webhook
from ..services.tokens import META_WEBHOOK_INTERNAL_TOKEN
from .common import MetaWebhookCase

WHATSAPP = "whatsapp_business_account"
CONSUMER = "test.whatsapp_cloud"


class TestMetaWebhookWhatsAppOwner(MetaWebhookCase):
    WABA_ID = "100000000000701"
    PHONE_ID = "100000000000702"
    WABA_TOKEN_REF = "ODOO_META_WEBHOOK_TEST_WABA_TOKEN"
    WABA_TOKEN = "synthetic-waba-token-12345"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._previous_waba = os.environ.get(cls.WABA_TOKEN_REF)
        os.environ[cls.WABA_TOKEN_REF] = cls.WABA_TOKEN
        cls.waba = cls.env["meta.webhook.page"].create(
            {
                "name": "Webhook test WhatsApp account",
                "endpoint_id": cls.endpoint.id,
                "owner_kind": WHATSAPP,
                "external_page_id": cls.WABA_ID,
                "credential_backend": "environment",
                "access_token_ref": cls.WABA_TOKEN_REF,
            }
        )

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            "web.base.url", "https://odoo.example.invalid"
        )

    @classmethod
    def tearDownClass(cls):
        if cls._previous_waba is None:
            os.environ.pop(cls.WABA_TOKEN_REF, None)
        else:
            os.environ[cls.WABA_TOKEN_REF] = cls._previous_waba
        super().tearDownClass()

    @classmethod
    def whatsapp_value(cls, messages=1, statuses=0, errors=0):
        customer = "5511988887777"
        return {
            "messaging_product": "whatsapp",
            "metadata": {
                "display_phone_number": "5511999990000",
                "phone_number_id": cls.PHONE_ID,
            },
            "contacts": [{"profile": {"name": "Cliente Sintético"}, "wa_id": customer}],
            "messages": [
                {
                    "from": customer,
                    "id": "wamid.message.%s" % index,
                    "timestamp": "1800000000",
                    "type": "image" if index else "text",
                    "text": {"body": "texto que nunca persiste no envelope"},
                    "image": {
                        "id": "900000000000%s" % index,
                        "mime_type": "image/jpeg",
                    },
                }
                for index in range(messages)
            ],
            "statuses": [
                {
                    "id": "wamid.status.%s" % index,
                    "status": "delivered",
                    "timestamp": "1800000000",
                    "recipient_id": customer,
                }
                for index in range(statuses)
            ],
            "errors": [
                {"code": 131000 + index, "title": "Something went wrong"}
                for index in range(errors)
            ],
        }

    @classmethod
    def whatsapp_envelope(cls, *values, other_field=False):
        changes = [{"field": "messages", "value": value} for value in values]
        if other_field:
            changes.append({"field": "account_update", "value": {"event": "X"}})
        return {
            "object": WHATSAPP,
            "entry": [{"id": cls.WABA_ID, "changes": changes}],
        }

    def _claiming(self, keys):
        def specs(dispatcher, endpoint, delivery, decoded):
            return [
                {
                    "item_key": key,
                    "object_type": WHATSAPP,
                    "event_field": "messages",
                    "target_asset_id": self.WABA_ID,
                    "occurrence_ref": "test:%s" % key,
                    "payload_json": {"schema_version": "test", "key": key},
                }
                for key in keys
            ]

        return patch.object(
            type(self.env["meta.webhook.dispatcher"]),
            "_consumer_item_specs",
            autospec=True,
            side_effect=specs,
        )

    def _subscribe(self):
        return self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.waba.id,
                "consumer_key": CONSUMER,
                "object_type": WHATSAPP,
                "field_name": "messages",
            }
        )

    # -- 1: sanitized envelope and atomic keys ---------------------------------

    def test_the_envelope_keeps_only_structure_and_declares_atomic_keys(self):
        sanitized = sanitized_webhook(
            self.whatsapp_envelope(
                self.whatsapp_value(messages=2, statuses=1, errors=1),
                other_field=True,
            )
        )
        persisted = json.dumps(sanitized.envelope, ensure_ascii=False)
        for private in ("5511", "Cliente", "nunca persiste", "wamid", "9000000"):
            self.assertNotIn(private, persisted)
        self.assertEqual(
            sanitized.envelope["entry"][0]["changes"][0],
            {
                "field": "messages",
                "change_index": 0,
                "messages_count": 2,
                "statuses_count": 1,
                "errors_count": 1,
            },
        )
        self.assertEqual(
            sanitized.expected_messaging_keys,
            (
                "entry:0:changes:0:messages:0",
                "entry:0:changes:0:messages:1",
                "entry:0:changes:0:statuses:0",
                "entry:0:changes:0:errors:0",
            ),
        )
        # Other WhatsApp fields stay unknown, as any unsupported change.
        self.assertEqual(len(sanitized.items), 1)
        self.assertEqual(sanitized.items[0]["kind"], "unknown")
        self.assertEqual(sanitized.items[0]["event_field"], "account_update")

    def test_the_aggregate_item_limit_counts_atomic_keys(self):
        full = self.whatsapp_value(messages=0)
        full["messages"] = [{} for _index in range(1000)]
        self.assertEqual(
            len(
                sanitized_webhook(self.whatsapp_envelope(full)).expected_messaging_keys
            ),
            1000,
        )
        concentrated = dict(full, messages=[{} for _index in range(1001)])
        spread = (
            dict(full, messages=[{} for _index in range(600)]),
            dict(full, messages=[], statuses=[{} for _index in range(401)]),
        )
        for envelope in (
            self.whatsapp_envelope(concentrated),
            self.whatsapp_envelope(*spread),
        ):
            with self.assertRaises(MetaWebhookSanitizationError):
                sanitized_webhook(envelope)

    def test_an_invalid_messages_value_is_kept_as_unknown(self):
        value = self.whatsapp_value()
        value["messages"] = "not a list"
        sanitized = sanitized_webhook(self.whatsapp_envelope(value))
        self.assertEqual(sanitized.expected_messaging_keys, ())
        self.assertEqual(
            [item["payload_json"]["reason"] for item in sanitized.items],
            ["invalid_whatsapp_messages"],
        )

    # -- 1b: persistence and sequences ------------------------------------------

    def test_every_atomic_key_is_persisted_once_with_disjoint_sequences(self):
        envelope = self.whatsapp_envelope(
            self.whatsapp_value(messages=2, statuses=1, errors=1),
            self.whatsapp_value(messages=1),
        )
        claimed = ("entry:0:changes:0:messages:1", "entry:0:changes:0:errors:0")
        with self._claiming(claimed):
            delivery = self.create_delivery(envelope)
        items = {item.item_key: item for item in delivery.item_ids}
        self.assertEqual(
            {key: item.sequence for key, item in items.items()},
            {
                "entry:0:changes:0:messages:0": 100_000_000,
                "entry:0:changes:0:messages:1": 100_000_001,
                "entry:0:changes:0:statuses:0": 100_001_000,
                "entry:0:changes:0:errors:0": 100_002_000,
                "entry:0:changes:1:messages:0": 100_010_000,
            },
        )
        self.assertEqual(
            {key for key, item in items.items() if item.kind == "messaging"},
            set(claimed),
        )
        placeholder = items["entry:0:changes:0:statuses:0"]
        self.assertEqual(placeholder.kind, "unknown")
        self.assertEqual(placeholder.event_field, "messages")
        self.assertEqual(
            placeholder.occurrence_ref, "wa:%s:0:statuses:0" % self.WABA_ID
        )
        self.assertEqual(placeholder.payload_json["reason"], "consumer_unavailable")
        self.assertEqual(delivery.item_count, 5)
        # Legacy sequences are unchanged.
        self.assertEqual(
            self.create_delivery(self.messaging_envelope()).item_ids.sequence, 500
        )
        self.assertEqual(
            self.create_delivery(self.leadgen_envelope()).item_ids.sequence, 0
        )

    def test_a_claim_must_keep_the_whatsapp_routing_contract(self):
        envelope = self.whatsapp_envelope(self.whatsapp_value())
        sanitized = sanitized_webhook(envelope)
        dispatcher = self.env["meta.webhook.dispatcher"]
        valid = {
            "item_key": "entry:0:changes:0:messages:0",
            "object_type": WHATSAPP,
            "event_field": "messages",
            "target_asset_id": self.WABA_ID,
            "occurrence_ref": "test",
            "payload_json": {"schema_version": "test"},
        }
        self.assertEqual(
            dispatcher._validated_messaging_spec(valid, sanitized)["sequence"],
            100_000_000,
        )
        for changes in (
            {"event_field": "statuses"},
            {"item_key": "entry:0:changes:0:messages:1"},
            {"target_asset_id": self.PHONE_ID},
            {"payload_json": {"media_url": "https://example.invalid/x"}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                dispatcher._validated_messaging_spec(dict(valid, **changes), sanitized)

    def test_erasing_one_claimed_item_keeps_the_others_and_the_hashes(self):
        self._subscribe()
        claimed = ("entry:0:changes:0:messages:0", "entry:0:changes:0:messages:1")
        with self._claiming(claimed):
            delivery = self.create_delivery(
                self.whatsapp_envelope(self.whatsapp_value(messages=2))
            )
        first, second = delivery.item_ids.sorted("sequence")
        digests = (first.event_sha256, second.event_sha256)
        erased = first.with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )._erase_consumer_message_content(CONSUMER)
        self.assertEqual(erased, first)
        self.assertEqual(first.payload_json["reason"], "consumer_content_erased")
        self.assertEqual(second.payload_json["key"], claimed[1])
        self.assertEqual((first.event_sha256, second.event_sha256), digests)

    def test_whatsapp_items_fan_out_only_to_messages_subscribers(self):
        with self._claiming(("entry:0:changes:0:messages:0",)):
            unrouted = self.create_delivery(
                self.whatsapp_envelope(self.whatsapp_value())
            )
        internal = unrouted.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        with trap_jobs():
            internal._fanout_once()
        self.assertEqual(unrouted.state, "unrouted")
        self._subscribe()
        with self._claiming(("entry:0:changes:0:messages:0",)):
            routed = self.create_delivery(
                self.whatsapp_envelope(self.whatsapp_value(), self.whatsapp_value())
            )
        with trap_jobs() as jobs:
            routed.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )._fanout_once()
        self.assertEqual(routed.state, "dispatched")
        self.assertEqual(routed.dispatch_ids.page_id, self.waba)
        self.assertEqual(set(routed.dispatch_ids.mapped("consumer_key")), {CONSUMER})
        jobs.assert_jobs_count(len(routed.dispatch_ids))

    # -- 2: owner, assets, subscriptions and reconciliation ---------------------

    def test_the_owner_kind_couples_assets_and_subscriptions(self):
        asset = self.waba.asset_ids
        self.assertEqual(
            (asset.platform, asset.object_type, asset.external_asset_id),
            ("whatsapp", WHATSAPP, self.WABA_ID),
        )
        self.assertEqual(self.waba.subscription_state, "manual")
        assets = self.env["meta.webhook.asset"]
        for owner, platform, object_type, external_id in (
            (self.waba, "facebook", "page", "100000000000703"),
            (self.waba, "whatsapp", WHATSAPP, "100000000000704"),
            (self.page, "whatsapp", WHATSAPP, "100000000000705"),
        ):
            with self.subTest(platform=platform), self.env.cr.savepoint():
                with self.assertRaises(ValidationError):
                    assets.create(
                        {
                            "page_id": owner.id,
                            "platform": platform,
                            "object_type": object_type,
                            "transport": "test",
                            "external_asset_id": external_id,
                        }
                    )
        self.assertEqual(self._subscribe().field_name, "messages")
        subscriptions = self.env["meta.webhook.subscription"]
        for owner, object_type, field_name in (
            (self.waba, WHATSAPP, "account_update"),
            (self.waba, "page", "messages"),
            (self.page, WHATSAPP, "messages"),
        ):
            with self.subTest(object_type=object_type, field=field_name):
                with self.env.cr.savepoint(), self.assertRaises(ValidationError):
                    subscriptions.create(
                        {
                            "page_id": owner.id,
                            "consumer_key": "test.other",
                            "object_type": object_type,
                            "field_name": field_name,
                        }
                    )
        with self.assertRaises(AccessError):
            self.waba.write({"owner_kind": "page"})
        revision = self.waba.revision
        self.waba.write({"access_token_ref": "ODOO_META_WEBHOOK_TEST_WABA_OTHER"})
        self.assertGreater(self.waba.revision, revision)
        self.assertEqual(self.waba.subscription_state, "manual")

    def test_reconciliation_never_plans_a_whatsapp_owner(self):
        self._subscribe()
        objects, plans, _digest = self.env[
            "meta.webhook.subscription.service"
        ]._configuration(self.endpoint)
        self.assertNotIn(WHATSAPP, objects)
        self.assertNotIn(self.waba.id, [plan["page_id"] for plan in plans])

    def test_manual_whatsapp_fields_neither_drift_nor_block_recovery(self):
        self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.page.id,
                "consumer_key": "test.lead_ads",
                "object_type": "page",
                "field_name": "leadgen",
            }
        )
        self._subscribe()
        service = self.env["meta.webhook.subscription.service"]
        _objects, _plans, digest = service._configuration(self.endpoint)
        methods = []

        def graph(_runtime, _token, method, path, **_kwargs):
            methods.append(method)
            if path.endswith("/subscriptions"):
                return {
                    "data": [
                        {
                            "object": object_type,
                            "callback_url": self.endpoint.webhook_url,
                            "fields": [field_name],
                            "active": True,
                        }
                        for object_type, field_name in (
                            ("page", "leadgen"),
                            (WHATSAPP, "messages"),
                        )
                    ]
                }
            return {
                "data": [
                    {"id": self.app.external_app_id, "subscribed_fields": ["leadgen"]}
                ]
            }

        with patch(
            "odoo.addons.meta_webhook_base.models.subscription_service.graph_request",
            side_effect=graph,
        ), patch.object(
            type(self.env["meta.webhook.dispatcher"]),
            "_after_subscription_reconcile",
            autospec=True,
            return_value=True,
        ) as recovered:
            result = service._reconcile_once(
                self.endpoint,
                expected_endpoint_revision=self.endpoint.revision,
                expected_app_revision=self.app.revision,
                expected_union_hash=digest,
            )
        self.assertTrue(result)
        self.assertNotIn("POST", methods, "nothing is rewritten")
        recovered.assert_called_once()
        self.assertEqual(self.endpoint.subscription_state, "in_sync")

    def test_an_endpoint_with_only_whatsapp_owners_never_calls_meta(self):
        os.environ["ODOO_META_WEBHOOK_TEST_WABA_VERIFY"] = "synthetic-verify-12345"
        self.addCleanup(os.environ.pop, "ODOO_META_WEBHOOK_TEST_WABA_VERIFY", None)
        app = self.env["meta.api.app"].create(
            {
                "name": "WhatsApp only App",
                "external_app_id": "100000000000708",
                "graph_version": "v26.0",
                "credential_backend": "environment",
                "app_secret_ref": self.APP_SECRET_REF,
            }
        )
        endpoint = self.env["meta.webhook.endpoint"].create(
            {
                "name": "WhatsApp only endpoint",
                "app_id": app.id,
                "credential_backend": "environment",
                "verify_token_ref": "ODOO_META_WEBHOOK_TEST_WABA_VERIFY",
            }
        )
        owner = self.env["meta.webhook.page"].create(
            {
                "name": "WhatsApp only account",
                "endpoint_id": endpoint.id,
                "owner_kind": WHATSAPP,
                "external_page_id": "100000000000709",
                "credential_backend": "environment",
                "access_token_ref": self.WABA_TOKEN_REF,
            }
        )
        self.env["meta.webhook.subscription"].create(
            {
                "page_id": owner.id,
                "consumer_key": CONSUMER,
                "object_type": WHATSAPP,
                "field_name": "messages",
            }
        )
        service = self.env["meta.webhook.subscription.service"]
        _objects, _plans, digest = service._configuration(endpoint)
        with patch(
            "odoo.addons.meta_webhook_base.models.subscription_service.graph_request"
        ) as graph, patch.object(
            type(endpoint), "_locked_runtime", autospec=True
        ) as runtime:
            result = service._reconcile_once(
                endpoint,
                expected_endpoint_revision=endpoint.revision,
                expected_app_revision=app.revision,
                expected_union_hash=digest,
            )
        self.assertFalse(result)
        graph.assert_not_called()
        runtime.assert_not_called()
        self.assertEqual(endpoint.subscription_state, "manual")
        self.assertTrue(endpoint.verified_at)
        # Archiving its last owner never turns the endpoint into a Meta caller.
        owner.write({"active": False})
        _objects, _plans, digest = service._configuration(endpoint)
        with patch(
            "odoo.addons.meta_webhook_base.models.subscription_service.graph_request"
        ) as graph, patch.object(
            type(endpoint), "_locked_runtime", autospec=True
        ) as runtime:
            service._reconcile_once(
                endpoint,
                expected_endpoint_revision=endpoint.revision,
                expected_app_revision=app.revision,
                expected_union_hash=digest,
            )
        graph.assert_not_called()
        runtime.assert_not_called()
        self.assertEqual(endpoint.subscription_state, "manual")

    def test_archiving_the_last_page_keeps_the_managed_readback(self):
        self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.page.id,
                "consumer_key": "test.lead_ads",
                "object_type": "page",
                "field_name": "leadgen",
            }
        )
        self._subscribe()
        self.page.write({"active": False})
        service = self.env["meta.webhook.subscription.service"]
        _objects, plans, digest = service._configuration(self.endpoint)
        self.assertFalse(plans)
        methods = []

        def graph(_runtime, _token, method, _path, **_kwargs):
            methods.append(method)
            return {
                "data": [
                    {
                        "object": "page",
                        "callback_url": self.endpoint.webhook_url,
                        "fields": ["leadgen"],
                        "active": True,
                    }
                ]
            }

        with patch(
            "odoo.addons.meta_webhook_base.models.subscription_service.graph_request",
            side_effect=graph,
        ):
            service._reconcile_once(
                self.endpoint,
                expected_endpoint_revision=self.endpoint.revision,
                expected_app_revision=self.app.revision,
                expected_union_hash=digest,
            )
        self.assertEqual(methods, ["GET"], "the retired Page is read back, not removed")
        self.assertEqual(self.endpoint.subscription_state, "drift")
        self.assertEqual(self.endpoint.last_error_class, "ManualRemovalRequired")

    # -- 2b: credentials ----------------------------------------------------------

    def test_page_credentials_are_refused_before_resolving_another_owner(self):
        with patch.object(page_module, "resolve_secret") as resolve:
            with self.assertRaises(MetaCredentialResolutionError):
                self.waba._resolved_access_token()
            with self.assertRaises(MetaCredentialResolutionError):
                self.page._resolved_access_token(expected_owner_kind=WHATSAPP)
        resolve.assert_not_called()
        self.assertEqual(
            self.waba._resolved_access_token(expected_owner_kind=WHATSAPP),
            self.WABA_TOKEN,
        )
        self.assertEqual(self.page._resolved_access_token(), self.PAGE_TOKEN)

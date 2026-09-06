from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from odoo import fields
from odoo.exceptions import ValidationError

from odoo.addons.meta_api_base.services.errors import (
    MetaApiError,
    MetaApiRateLimitError,
    MetaApiTransientError,
    MetaApiUncertainError,
)
from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.contracts import META_WEBHOOK_FRESHNESS, META_WEBHOOK_REFRESH_AFTER
from ..services.tokens import META_WEBHOOK_INTERNAL_TOKEN
from .common import MetaWebhookCase


class TestMetaWebhookSubscriptions(MetaWebhookCase):
    JOB_UUID = "30000000-0000-4000-8000-000000000003"
    OTHER_JOB_UUID = "40000000-0000-4000-8000-000000000004"

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            "web.base.url", "https://odoo.example.invalid"
        )

    def _subscribe(self, consumer, object_type, field_name):
        return self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.page.id,
                "consumer_key": consumer,
                "object_type": object_type,
                "field_name": field_name,
            }
        )

    def test_endpoint_action_enqueues_and_returns_rpc_safe_job_uuid(self):
        job_uuid = UUID("65d2ceab-18b6-46a9-b257-35537ed46aa5")
        service_class = type(self.env["meta.webhook.subscription.service"])
        with patch.object(
            service_class,
            "_enqueue_endpoint",
            autospec=True,
            return_value=SimpleNamespace(uuid=job_uuid),
        ) as enqueue:
            result = self.endpoint.action_enqueue_subscription_reconcile()

        self.assertEqual(result, str(job_uuid))
        self.assertIsInstance(result, str)
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], self.endpoint)

    def test_page_action_enqueues_endpoint_and_returns_rpc_safe_job_uuid(self):
        job_uuid = UUID("b9a4b06c-4c9d-4581-90ea-50d48dd71d22")
        service_class = type(self.env["meta.webhook.subscription.service"])
        with patch.object(
            service_class,
            "_enqueue_endpoint",
            autospec=True,
            return_value=SimpleNamespace(uuid=job_uuid),
        ) as enqueue:
            result = self.page.action_enqueue_subscription_reconcile()

        self.assertEqual(result, str(job_uuid))
        self.assertIsInstance(result, str)
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], self.endpoint)

    def test_enqueue_revalidates_locked_database_state(self):
        self.assertTrue(self.endpoint.active)
        self.endpoint.flush_recordset(["active"])
        self.env.cr.execute(
            "UPDATE meta_webhook_endpoint SET active = FALSE WHERE id = %s",
            [self.endpoint.id],
        )

        with self.assertRaisesRegex(ValidationError, "endpoint or App is paused"):
            self.env["meta.webhook.subscription.service"]._enqueue_endpoint(
                self.endpoint
            )

        self.endpoint.invalidate_recordset(["active"])
        self.assertFalse(self.endpoint.active)

    def test_enqueue_locks_endpoint_before_app(self):
        service = self.env["meta.webhook.subscription.service"]
        cursor_class = type(self.env.cr)
        original_execute = cursor_class.execute
        locks = []

        def traced_execute(cursor, query, *args, **kwargs):
            normalized = " ".join(str(query).lower().split())
            if "for update" in normalized and "meta_webhook_endpoint" in normalized:
                locks.append("endpoint")
            if "for share" in normalized and "meta_api_app" in normalized:
                locks.append("app")
            return original_execute(cursor, query, *args, **kwargs)

        with patch.object(cursor_class, "execute", traced_execute), trap_jobs():
            service._enqueue_endpoint(self.endpoint)

        self.assertGreaterEqual(len(locks), 2)
        self.assertEqual(locks[:2], ["endpoint", "app"])

    def test_job_identity_includes_both_configuration_fences(self):
        service = self.env["meta.webhook.subscription.service"]
        first = service._identity_key(self.endpoint, 3, 7, "a" * 64)
        endpoint_rotated = service._identity_key(self.endpoint, 4, 7, "a" * 64)
        app_rotated = service._identity_key(self.endpoint, 3, 8, "a" * 64)

        self.assertNotEqual(first, endpoint_rotated)
        self.assertNotEqual(first, app_rotated)
        self.assertTrue(first.endswith(":3:7:%s" % ("a" * 16)))

    def test_app_change_invalidates_dependent_observation_without_lock_inversion(self):
        internal = self.endpoint.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        internal.write(
            {
                "subscription_state": "in_sync",
                "observed_subscriptions_json": [{"object": "page"}],
                "verified_at": fields.Datetime.now(),
            }
        )
        self.page.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        ).write(
            {
                "subscription_state": "in_sync",
                "observed_fields_json": ["messages"],
                "verified_at": fields.Datetime.now(),
            }
        )
        endpoint_revision = self.endpoint.revision
        cursor_class = type(self.env.cr)
        original_execute = cursor_class.execute
        locks = []

        def traced_execute(cursor, query, *args, **kwargs):
            normalized = " ".join(str(query).lower().split())
            if "for update" in normalized:
                if "from meta_webhook_endpoint" in normalized:
                    locks.append("endpoint")
                elif "from meta_api_app" in normalized:
                    locks.append("app")
            return original_execute(cursor, query, *args, **kwargs)

        with patch.object(cursor_class, "execute", traced_execute):
            self.app.write({"graph_version": "v25.0"})

        self.endpoint.invalidate_recordset()
        self.assertEqual(locks[:2], ["endpoint", "app"])
        self.assertEqual(self.endpoint.revision, endpoint_revision)
        self.assertEqual(self.endpoint.subscription_state, "unknown")
        self.assertFalse(self.endpoint.observed_subscriptions_json)
        self.assertFalse(self.endpoint.verified_at)
        self.page.invalidate_recordset()
        self.assertEqual(self.page.subscription_state, "unknown")
        self.assertFalse(self.page.observed_fields_json)
        self.assertFalse(self.page.verified_at)

    def test_app_pause_and_resume_project_honest_endpoint_state(self):
        internal = self.endpoint.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        internal.write(
            {
                "subscription_state": "in_sync",
                "observed_subscriptions_json": [{"object": "page"}],
                "verified_at": fields.Datetime.now(),
            }
        )
        page_internal = self.page.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        page_internal.write(
            {
                "subscription_state": "in_sync",
                "observed_fields_json": ["messages"],
                "verified_at": fields.Datetime.now(),
            }
        )

        self.app.write({"active": False})
        self.endpoint.invalidate_recordset()
        self.assertEqual(self.endpoint.subscription_state, "error")
        self.assertEqual(self.endpoint.last_error_class, "AppPaused")
        self.assertFalse(self.endpoint.verified_at)
        self.page.invalidate_recordset()
        self.assertEqual(self.page.subscription_state, "error")
        self.assertEqual(self.page.last_error_class, "AppPaused")
        self.assertFalse(self.page.observed_fields_json)
        self.assertFalse(self.page.verified_at)

        self.app.write({"active": True})
        self.endpoint.invalidate_recordset()
        self.assertEqual(self.endpoint.subscription_state, "unknown")
        self.assertFalse(self.endpoint.last_error_class)
        self.assertFalse(self.endpoint.last_error_message)
        self.page.invalidate_recordset()
        self.assertEqual(self.page.subscription_state, "unknown")
        self.assertFalse(self.page.last_error_class)
        self.assertFalse(self.page.last_error_message)

    def test_subscription_retry_respects_bounded_provider_delay(self):
        service = self.env["meta.webhook.subscription.service"].with_context(
            job_uuid=self.JOB_UUID
        )
        service_class = type(service)
        self.endpoint.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        ).write({"queue_job_uuid": self.JOB_UUID})
        values = {
            "expected_endpoint_revision": self.endpoint.revision,
            "expected_app_revision": self.app.revision,
            "expected_union_hash": "a" * 64,
        }
        for error, expected_seconds in (
            (MetaApiRateLimitError("limited", retry_after_seconds=900), 900),
            (MetaApiTransientError("transient", retry_after_seconds=120), 120),
            (MetaApiTransientError("transient", retry_after_seconds=0), None),
        ):
            with self.subTest(error=type(error).__name__), patch.object(
                service_class,
                "_reconcile_once",
                autospec=True,
                side_effect=error,
            ), self.assertRaises(RetryableJobError) as caught:
                service._job_reconcile_endpoint(self.endpoint, **values)

            self.assertEqual(caught.exception.seconds, expected_seconds)

    def test_subscription_worker_requires_present_and_matching_job_uuid(self):
        values = {
            "expected_endpoint_revision": self.endpoint.revision,
            "expected_app_revision": self.app.revision,
            "expected_union_hash": "a" * 64,
        }
        service = self.env["meta.webhook.subscription.service"].with_context(
            job_uuid=self.JOB_UUID
        )
        service_class = type(service)
        internal = self.endpoint.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        with patch.object(service_class, "_reconcile_once", autospec=True) as reconcile:
            self.assertFalse(service._job_reconcile_endpoint(self.endpoint, **values))
            internal.write({"queue_job_uuid": self.OTHER_JOB_UUID})
            self.assertFalse(service._job_reconcile_endpoint(self.endpoint, **values))

        reconcile.assert_not_called()

    def test_subscription_worker_uses_webhook_compatible_ownership_lock(self):
        """Remote reconciliation must not exclude public webhook snapshots."""

        service = self.env["meta.webhook.subscription.service"].with_context(
            job_uuid=self.JOB_UUID
        )
        internal = self.endpoint.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        internal.write({"queue_job_uuid": self.JOB_UUID})
        values = {
            "expected_endpoint_revision": self.endpoint.revision,
            "expected_app_revision": self.app.revision,
            "expected_union_hash": "a" * 64,
        }
        cursor_class = type(self.env.cr)
        original_execute = cursor_class.execute
        worker_locks = []

        def traced_execute(cursor, query, *args, **kwargs):
            normalized = " ".join(str(query).lower().split())
            if "pg_advisory_xact_lock" in normalized:
                worker_locks.append(("advisory", normalized))
            if (
                "select queue_job_uuid from meta_webhook_endpoint" in normalized
                and "where id = %s" in normalized
            ):
                worker_locks.append(("ownership", normalized))
            return original_execute(cursor, query, *args, **kwargs)

        service_class = type(service)
        with patch.object(cursor_class, "execute", traced_execute), patch.object(
            service_class,
            "_reconcile_once",
            autospec=True,
            return_value=False,
        ):
            self.assertFalse(service._job_reconcile_endpoint(self.endpoint, **values))

        self.assertEqual(
            [kind for kind, _query in worker_locks], ["advisory", "ownership"]
        )
        self.assertIn("for share", worker_locks[1][1])
        self.assertNotIn("for update", worker_locks[1][1])

    def test_periodic_reconcile_refreshes_inside_public_freshness_window(self):
        self.assertEqual(META_WEBHOOK_FRESHNESS, timedelta(minutes=30))
        self.assertLess(META_WEBHOOK_REFRESH_AFTER, META_WEBHOOK_FRESHNESS)
        now = fields.Datetime.to_datetime(fields.Datetime.now())
        internal = self.endpoint.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        internal.write(
            {
                "subscription_state": "in_sync",
                "verified_at": now,
            }
        )
        service_class = type(self.env["meta.webhook.subscription.service"])
        with patch.object(
            service_class,
            "_enqueue_endpoint",
            autospec=True,
            return_value=SimpleNamespace(uuid=UUID(int=1)),
        ) as enqueue:
            result = self.env["meta.webhook.endpoint"]._cron_reconcile_subscriptions(
                limit=20,
                refresh_seconds=10 * 60,
                now=now,
            )
            self.assertEqual(result, 0)
            enqueue.assert_not_called()

            internal.write({"verified_at": now - timedelta(minutes=11)})
            result = self.env["meta.webhook.endpoint"]._cron_reconcile_subscriptions(
                limit=20,
                refresh_seconds=10 * 60,
                now=now,
            )

        self.assertEqual(result, 1)
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], self.endpoint)

    def test_periodic_reconcile_immediately_selects_error_and_unknown(self):
        now = fields.Datetime.to_datetime(fields.Datetime.now())
        internal = self.endpoint.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        service_class = type(self.env["meta.webhook.subscription.service"])
        for state in ("error", "unknown"):
            internal.write(
                {
                    "subscription_state": state,
                    "verified_at": now,
                }
            )
            with patch.object(
                service_class,
                "_enqueue_endpoint",
                autospec=True,
                return_value=SimpleNamespace(uuid=UUID(int=2)),
            ) as enqueue:
                result = self.env[
                    "meta.webhook.endpoint"
                ]._cron_reconcile_subscriptions(
                    limit=20,
                    refresh_seconds=10 * 60,
                    now=now,
                )
            self.assertEqual(result, 1)
            enqueue.assert_called_once()

    def test_periodic_reconcile_is_bounded(self):
        second_app = self.env["meta.api.app"].create(
            {
                "name": "Second webhook test App",
                "external_app_id": "100000000000002",
                "graph_version": "v26.0",
                "credential_backend": "environment",
                "app_secret_ref": self.APP_SECRET_REF,
            }
        )
        self.env["meta.webhook.endpoint"].create(
            {
                "name": "Second webhook test endpoint",
                "app_id": second_app.id,
                "credential_backend": "environment",
                "verify_token_ref": self.VERIFY_TOKEN_REF,
            }
        )
        service_class = type(self.env["meta.webhook.subscription.service"])
        with patch.object(
            service_class,
            "_enqueue_endpoint",
            autospec=True,
            return_value=SimpleNamespace(uuid=UUID(int=3)),
        ) as enqueue:
            result = self.env["meta.webhook.endpoint"]._cron_reconcile_subscriptions(
                limit=1
            )

        self.assertEqual(result, 1)
        self.assertEqual(enqueue.call_count, 1)
        with self.assertRaises(ValidationError):
            self.env["meta.webhook.endpoint"]._cron_reconcile_subscriptions(limit=0)

    def test_periodic_reconcile_skips_covered_jobs_without_starving_next(self):
        second_app = self.env["meta.api.app"].create(
            {
                "name": "Fairness webhook test App",
                "external_app_id": "100000000000003",
                "graph_version": "v26.0",
                "credential_backend": "environment",
                "app_secret_ref": self.APP_SECRET_REF,
            }
        )
        second_endpoint = self.env["meta.webhook.endpoint"].create(
            {
                "name": "Fairness webhook test endpoint",
                "app_id": second_app.id,
                "credential_backend": "environment",
                "verify_token_ref": self.VERIFY_TOKEN_REF,
            }
        )
        self.endpoint.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        ).write({"queue_job_uuid": "covered-job"})
        queue_job_class = type(self.env["queue.job"])
        service_class = type(self.env["meta.webhook.subscription.service"])
        active_jobs = SimpleNamespace(mapped=lambda _field: ["covered-job"])
        with patch.object(
            queue_job_class,
            "search",
            autospec=True,
            return_value=active_jobs,
        ), patch.object(
            service_class,
            "_enqueue_endpoint",
            autospec=True,
            return_value=SimpleNamespace(uuid=UUID(int=4)),
        ) as enqueue:
            result = self.env["meta.webhook.endpoint"]._cron_reconcile_subscriptions(
                limit=1
            )

        self.assertEqual(result, 1)
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], second_endpoint)

    def test_configuration_is_global_union_not_consumer_specific(self):
        self._subscribe("contact.meta", "page", "messages")
        self._subscribe("marketing.lead_ads", "page", "leadgen")
        self._subscribe("contact.instagram", "instagram", "messaging_seen")
        objects, plans, _digest = self.env[
            "meta.webhook.subscription.service"
        ]._configuration(self.endpoint)
        self.assertEqual(objects["page"], ("leadgen", "messages"))
        self.assertEqual(objects["instagram"], ("messaging_seen",))
        self.assertEqual(plans[0]["fields"], ("leadgen", "messages"))
        self.assertTrue(plans[0]["required"])

    def test_reconcile_writes_app_union_then_page_union_and_proves_readback(self):
        self._subscribe("marketing.lead_ads", "page", "leadgen")
        self._subscribe("contact.instagram", "instagram", "messages")
        service = self.env["meta.webhook.subscription.service"]
        objects, _plans, digest = service._configuration(self.endpoint)
        app_posts = {}
        page_installed = [False]

        def graph(_runtime, _token, method, path, **kwargs):
            if path.endswith("/subscriptions"):
                if method == "POST":
                    app_posts[kwargs["data"]["object"]] = kwargs["data"]
                    return {"success": True}
                return {
                    "data": (
                        [
                            {
                                "object": key,
                                "callback_url": self.endpoint.webhook_url,
                                "fields": list(values),
                                "active": True,
                            }
                            for key, values in sorted(objects.items())
                        ]
                        if app_posts
                        else []
                    )
                }
            if method == "POST":
                page_installed[0] = True
                self.assertEqual(
                    kwargs["data"],
                    {"subscribed_fields": "leadgen"},
                )
                return {"success": True}
            return {
                "data": (
                    [
                        {
                            "id": self.app.external_app_id,
                            "subscribed_fields": ["leadgen"],
                        }
                    ]
                    if page_installed[0]
                    else []
                )
            }

        dispatcher_class = type(self.env["meta.webhook.dispatcher"])
        with patch(
            "odoo.addons.meta_webhook_base.models.subscription_service.graph_request",
            side_effect=graph,
        ), patch.object(
            dispatcher_class,
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
        recovered.assert_called_once()
        self.assertEqual(recovered.call_args.args[1], self.endpoint)
        self.assertEqual(set(app_posts), {"page", "instagram"})
        self.assertEqual(app_posts["page"]["fields"], "leadgen")
        self.assertEqual(app_posts["instagram"]["fields"], "messages")
        self.assertEqual(self.endpoint.subscription_state, "in_sync")
        self.assertEqual(self.page.subscription_state, "in_sync")

    def test_instagram_only_does_not_send_instagram_fields_to_page_edge(self):
        self._subscribe("contact.instagram", "instagram", "messages")
        self._subscribe("contact.instagram", "instagram", "messaging_seen")
        service = self.env["meta.webhook.subscription.service"]
        objects, plans, digest = service._configuration(self.endpoint)
        self.assertEqual(objects, {"instagram": ("messages", "messaging_seen")})
        self.assertFalse(plans[0]["required"])
        self.assertEqual(plans[0]["fields"], ())

        app_installed = [False]
        page_posts = []

        def graph(_runtime, _token, method, path, **kwargs):
            if path.endswith("/subscriptions"):
                if method == "POST":
                    app_installed[0] = True
                    self.assertEqual(kwargs["data"]["object"], "instagram")
                    self.assertEqual(
                        kwargs["data"]["fields"], "messages,messaging_seen"
                    )
                    return {"success": True}
                return {
                    "data": (
                        [
                            {
                                "object": "instagram",
                                "callback_url": self.endpoint.webhook_url,
                                "fields": ["messages", "messaging_seen"],
                                "active": True,
                            }
                        ]
                        if app_installed[0]
                        else []
                    )
                }
            if method == "POST":
                page_posts.append(kwargs["data"])
                return {"success": True}
            return {"data": []}

        with patch(
            "odoo.addons.meta_webhook_base.models.subscription_service.graph_request",
            side_effect=graph,
        ):
            result = service._reconcile_once(
                self.endpoint,
                expected_endpoint_revision=self.endpoint.revision,
                expected_app_revision=self.app.revision,
                expected_union_hash=digest,
            )

        self.assertTrue(result)
        self.assertEqual(page_posts, [])
        self.assertEqual(self.endpoint.subscription_state, "in_sync")
        self.assertEqual(self.page.subscription_state, "in_sync")

    def test_stale_union_never_reaches_graph(self):
        service = self.env["meta.webhook.subscription.service"]
        with patch(
            "odoo.addons.meta_webhook_base.models.subscription_service.graph_request"
        ) as graph:
            result = service._reconcile_once(
                self.endpoint,
                expected_endpoint_revision=self.endpoint.revision,
                expected_app_revision=self.app.revision,
                expected_union_hash="0" * 64,
            )
        self.assertFalse(result)
        graph.assert_not_called()

    def test_empty_union_performs_readback_without_destructive_mutation(self):
        service = self.env["meta.webhook.subscription.service"]
        _objects, _plans, digest = service._configuration(self.endpoint)
        methods = []

        def graph(_runtime, _token, method, _path, **_kwargs):
            methods.append(method)
            return {"data": []}

        with patch(
            "odoo.addons.meta_webhook_base.models.subscription_service.graph_request",
            side_effect=graph,
        ):
            result = service._reconcile_once(
                self.endpoint,
                expected_endpoint_revision=self.endpoint.revision,
                expected_app_revision=self.app.revision,
                expected_union_hash=digest,
            )
        self.assertTrue(result)
        self.assertNotIn("POST", methods)

    def test_uncertain_app_mutation_is_never_blindly_replayed(self):
        self._subscribe("marketing.lead_ads", "page", "leadgen")
        service = self.env["meta.webhook.subscription.service"]
        _objects, _plans, digest = service._configuration(self.endpoint)

        def graph(_runtime, _token, method, _path, **_kwargs):
            if method == "POST":
                raise MetaApiUncertainError("synthetic uncertain mutation")
            return {"data": []}

        with patch(
            "odoo.addons.meta_webhook_base.models.subscription_service.graph_request",
            side_effect=graph,
        ), self.assertRaises(MetaApiUncertainError):
            service._reconcile_once(
                self.endpoint,
                expected_endpoint_revision=self.endpoint.revision,
                expected_app_revision=self.app.revision,
                expected_union_hash=digest,
            )

    def test_page_installation_uses_bounded_cursor_pagination(self):
        service = self.env["meta.webhook.subscription.service"]
        pages = (
            {
                "data": [{"id": "999"}],
                "paging": {
                    "cursors": {"after": "second-page"},
                    "next": "https://graph.facebook.com/next-page",
                },
            },
            {
                "data": [
                    {
                        "id": self.app.external_app_id,
                        "subscribed_fields": ["messages", "leadgen"],
                    }
                ]
            },
        )

        with patch(
            "odoo.addons.meta_webhook_base.models.subscription_service.graph_request",
            side_effect=pages,
        ) as graph:
            installed, fields_union = service._read_page_installation(
                self.app,
                self.PAGE_TOKEN,
                self.PAGE_ID,
            )

        self.assertTrue(installed)
        self.assertEqual(fields_union, ("leadgen", "messages"))
        self.assertEqual(graph.call_count, 2)
        self.assertEqual(graph.call_args_list[0].kwargs["params"], {"limit": 100})
        self.assertEqual(
            graph.call_args_list[1].kwargs["params"],
            {"limit": 100, "after": "second-page"},
        )

    def test_page_installation_rejects_nonadvancing_cursor(self):
        service = self.env["meta.webhook.subscription.service"]
        payload = {
            "data": [],
            "paging": {
                "cursors": {"after": "same-page"},
                "next": "https://graph.facebook.com/next-page",
            },
        }

        with patch(
            "odoo.addons.meta_webhook_base.models.subscription_service.graph_request",
            side_effect=(payload, payload),
        ), self.assertRaisesRegex(MetaApiError, "did not advance"):
            service._read_page_installation(
                self.app,
                self.PAGE_TOKEN,
                self.PAGE_ID,
            )

    def test_subscription_reads_stop_at_terminal_cursors(self):
        service = self.env["meta.webhook.subscription.service"]
        payload = {
            "data": [],
            "paging": {"cursors": {"before": "first", "after": "last"}},
        }
        with patch(
            "odoo.addons.meta_webhook_base.models.subscription_service.graph_request",
            return_value=payload,
        ) as graph:
            self.assertEqual(
                service._read_page_installation(
                    self.app, self.PAGE_TOKEN, self.PAGE_ID
                ),
                (False, ()),
            )
            graph.assert_called_once()
            graph.reset_mock()
            self.assertEqual(service._read_app_subscriptions(self.app, "app-token"), ())
            graph.assert_called_once()

    def test_app_subscription_rejects_ambiguous_active_state(self):
        payload = {
            "data": [
                {
                    "object": "page",
                    "callback_url": self.endpoint.webhook_url,
                    "fields": ["messages"],
                    "active": "false",
                }
            ]
        }
        with patch(
            "odoo.addons.meta_webhook_base.models.subscription_service.graph_request",
            return_value=payload,
        ), self.assertRaisesRegex(MetaApiError, "response is invalid"):
            self.env["meta.webhook.subscription.service"]._read_app_subscriptions(
                self.app, self.PAGE_TOKEN
            )

    def test_app_sync_rejects_duplicate_provider_objects(self):
        service = self.env["meta.webhook.subscription.service"]
        observed = (
            {
                "object": "page",
                "callback_url": self.endpoint.webhook_url,
                "fields": ("messages",),
                "active": True,
            },
            {
                "object": "page",
                "callback_url": self.endpoint.webhook_url,
                "fields": ("messages",),
                "active": True,
            },
        )

        self.assertFalse(
            service._app_is_in_sync(
                {"page": ("messages",)}, observed, self.endpoint.webhook_url
            )
        )

    def test_page_error_projection_clears_stale_observation(self):
        internal = self.page.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        internal.write({"observed_fields_json": ["messages"]})

        projected = self.env["meta.webhook.subscription.service"]._project_page(
            self.page,
            self.page.revision,
            "error",
            "ProviderError",
            "Meta rejected the Page installation configuration.",
        )

        self.assertTrue(projected)
        self.assertFalse(self.page.observed_fields_json)

    def test_page_installation_rejects_invalid_provider_field(self):
        payload = {
            "data": [
                {
                    "id": self.app.external_app_id,
                    "subscribed_fields": ["messages\nforged"],
                }
            ]
        }
        with patch(
            "odoo.addons.meta_webhook_base.models.subscription_service.graph_request",
            return_value=payload,
        ), self.assertRaisesRegex(MetaApiError, "response is invalid"):
            self.env["meta.webhook.subscription.service"]._read_page_installation(
                self.app, self.PAGE_TOKEN, self.PAGE_ID
            )

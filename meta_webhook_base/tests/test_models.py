import json
import pickle
from datetime import timedelta
from unittest.mock import patch

from psycopg2 import OperationalError
from psycopg2.errors import SerializationFailure

from odoo import fields
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.meta_api_base.services.credentials import MetaCredentialResolutionError
from odoo.addons.queue_job.exception import FailedJobError, RetryableJobError
from odoo.addons.queue_job.job import Job
from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.tokens import META_WEBHOOK_INTERNAL_TOKEN, META_WEBHOOK_RUNTIME_TOKEN
from .common import MetaWebhookCase


class TestMetaWebhookModels(MetaWebhookCase):
    DELIVERY_JOB_UUID = "10000000-0000-4000-8000-000000000001"
    DISPATCH_JOB_UUID = "20000000-0000-4000-8000-000000000002"

    def test_internal_capabilities_are_process_local(self):
        for token in (META_WEBHOOK_INTERNAL_TOKEN, META_WEBHOOK_RUNTIME_TOKEN):
            with self.subTest(token=repr(token)):
                with self.assertRaises(TypeError):
                    pickle.dumps(token)
                with self.assertRaises(TypeError):
                    json.dumps({"capability": token})

    def test_delivery_job_reanchors_the_owning_company(self):
        caller_company = self.env["res.company"].create(
            {"name": "Unrelated webhook worker company"}
        )
        delivery = self.create_delivery(self.leadgen_envelope())
        internal = delivery.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        internal.write({"queue_job_uuid": self.DELIVERY_JOB_UUID})
        observed = []

        def inspect_scope(scoped_delivery):
            observed.append(
                (
                    scoped_delivery.env.company.id,
                    scoped_delivery.env.companies.ids,
                    scoped_delivery.company_id.id,
                )
            )
            return True

        delivery_class = type(delivery)
        wrong_scope = internal.with_company(caller_company).with_context(
            allowed_company_ids=[caller_company.id],
            job_uuid=self.DELIVERY_JOB_UUID,
        )
        with patch.object(
            delivery_class,
            "_fanout_once",
            autospec=True,
            side_effect=inspect_scope,
        ):
            self.assertTrue(wrong_scope._job_fanout())

        self.assertEqual(
            observed,
            [
                (
                    self.endpoint.company_id.id,
                    [self.endpoint.company_id.id],
                    self.endpoint.company_id.id,
                )
            ],
        )

    def test_dispatch_job_reanchors_consumer_to_the_owning_company(self):
        caller_company = self.env["res.company"].create(
            {"name": "Unrelated webhook consumer company"}
        )
        self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.page.id,
                "consumer_key": "marketing.lead_ads",
                "object_type": "page",
                "field_name": "leadgen",
            }
        )
        delivery = self.create_delivery(self.leadgen_envelope())
        with trap_jobs():
            delivery.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )._fanout_once()
        dispatch = delivery.dispatch_ids.ensure_one()
        internal = dispatch.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        internal.write({"queue_job_uuid": self.DISPATCH_JOB_UUID})
        observed = []

        def inspect_scope(dispatcher, scoped_dispatch):
            observed.append(
                (
                    dispatcher.env.company.id,
                    dispatcher.env.companies.ids,
                    scoped_dispatch.env.company.id,
                    scoped_dispatch.env.companies.ids,
                    scoped_dispatch.company_id.id,
                )
            )
            return {"handled": True, "result_ref": "test:company-scope"}

        dispatcher_class = type(self.env["meta.webhook.dispatcher"])
        wrong_scope = internal.with_company(caller_company).with_context(
            allowed_company_ids=[caller_company.id],
            job_uuid=self.DISPATCH_JOB_UUID,
        )
        with patch.object(
            dispatcher_class,
            "_dispatch_consumer",
            autospec=True,
            side_effect=inspect_scope,
        ):
            self.assertTrue(wrong_scope._job_process())

        owning_company = self.endpoint.company_id.id
        self.assertEqual(
            observed,
            [
                (
                    owning_company,
                    [owning_company],
                    owning_company,
                    [owning_company],
                    owning_company,
                )
            ],
        )

    def test_page_creation_registers_facebook_page_alias(self):
        self.assertEqual(len(self.page.asset_ids), 1)
        alias = self.page.asset_ids
        self.assertEqual(alias.platform, "facebook")
        self.assertEqual(alias.object_type, "page")
        self.assertEqual(alias.external_asset_id, self.PAGE_ID)

    def test_configuration_create_rejects_managed_identity_and_runtime_state(self):
        endpoint_values = {
            "name": "Injected endpoint state",
            "app_id": self.app.id,
            "credential_backend": "environment",
            "verify_token_ref": self.VERIFY_TOKEN_REF,
        }
        for field_name, value in (
            ("routing_key", "a" * 32),
            ("subscription_state", "in_sync"),
            ("queue_job_uuid", "00000000-0000-4000-8000-000000000001"),
        ):
            with self.subTest(model="endpoint", field=field_name), self.assertRaises(
                AccessError
            ):
                self.env["meta.webhook.endpoint"].create(
                    {**endpoint_values, field_name: value}
                )

        page_values = {
            "name": "Injected Page state",
            "endpoint_id": self.endpoint.id,
            "external_page_id": "100000000000109",
            "credential_backend": "environment",
            "access_token_ref": self.PAGE_TOKEN_REF,
        }
        for field_name, value in (
            ("subscription_state", "in_sync"),
            ("observed_fields_json", ["messages"]),
        ):
            with self.subTest(model="page", field=field_name), self.assertRaises(
                AccessError
            ):
                self.env["meta.webhook.page"].create({**page_values, field_name: value})

    def test_creation_under_paused_app_projects_honest_state(self):
        app = self.env["meta.api.app"].create(
            {
                "name": "Paused webhook App",
                "active": False,
                "external_app_id": "100000000000009",
                "credential_backend": "environment",
                "app_secret_ref": self.APP_SECRET_REF,
            }
        )
        endpoint = self.env["meta.webhook.endpoint"].create(
            {
                "name": "Paused App endpoint",
                "app_id": app.id,
                "credential_backend": "environment",
                "verify_token_ref": self.VERIFY_TOKEN_REF,
            }
        )
        page = self.env["meta.webhook.page"].create(
            {
                "name": "Paused App Page",
                "endpoint_id": endpoint.id,
                "external_page_id": "100000000000209",
                "credential_backend": "environment",
                "access_token_ref": self.PAGE_TOKEN_REF,
            }
        )

        self.assertEqual(endpoint.subscription_state, "error")
        self.assertEqual(endpoint.last_error_class, "AppPaused")
        self.assertEqual(page.subscription_state, "error")
        self.assertEqual(page.last_error_class, "AppPaused")

        app.write({"active": True})
        endpoint.invalidate_recordset()
        page.invalidate_recordset()
        self.assertEqual(endpoint.subscription_state, "unknown")
        self.assertEqual(page.subscription_state, "unknown")

    def test_instagram_asset_routes_to_owning_page(self):
        instagram_id = "178400000000001"
        self.env["meta.webhook.asset"].create(
            {
                "page_id": self.page.id,
                "platform": "instagram",
                "object_type": "instagram",
                "transport": "page_linked",
                "external_asset_id": instagram_id,
            }
        )
        self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.page.id,
                "consumer_key": "contact_center.meta",
                "object_type": "instagram",
                "field_name": "messages",
            }
        )
        delivery = self.create_delivery(
            self.messaging_envelope("instagram", instagram_id)
        )
        with trap_jobs() as jobs:
            delivery.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )._fanout_once()
        jobs.assert_jobs_count(1)
        self.assertEqual(delivery.state, "dispatched")
        self.assertEqual(delivery.dispatch_ids.page_id, self.page)

    def test_asset_routing_is_namespaced_by_meta_object(self):
        """A Page ID must never impersonate a linked Instagram asset."""

        self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.page.id,
                "consumer_key": "contact_center.meta",
                "object_type": "instagram",
                "field_name": "messages",
            }
        )
        delivery = self.create_delivery(
            self.messaging_envelope("instagram", self.PAGE_ID)
        )

        with trap_jobs() as jobs:
            delivery.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )._fanout_once()

        jobs.assert_jobs_count(0)
        self.assertEqual(delivery.state, "unrouted")
        self.assertFalse(delivery.dispatch_ids)

        instagram = self.env["meta.webhook.asset"].create(
            {
                "page_id": self.page.id,
                "platform": "instagram",
                "object_type": "instagram",
                "transport": "page_linked",
                "external_asset_id": self.PAGE_ID,
            }
        )
        self.assertEqual(instagram.external_asset_id, self.page.external_page_id)

    def test_asset_identity_is_immutable_even_for_internal_callers(self):
        asset = self.page.asset_ids.ensure_one()
        identity = {
            "page_id": asset.page_id.id,
            "external_asset_id": asset.external_asset_id,
            "platform": asset.platform,
            "object_type": asset.object_type,
            "transport": asset.transport,
        }
        internal = asset.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        for field_name, value in identity.items():
            with self.subTest(field_name=field_name), self.assertRaises(AccessError):
                internal.write({field_name: value})

        asset.write({"active": False})
        self.assertFalse(asset.active)

    def test_leadgen_fanout_is_deduplicated_by_consumer(self):
        for field_name in ("leadgen", "leadgen"):
            values = {
                "page_id": self.page.id,
                "consumer_key": "marketing.lead_ads",
                "object_type": "page",
                "field_name": field_name,
            }
            if not self.env["meta.webhook.subscription"].search_count(
                [(key, "=", value) for key, value in values.items()]
            ):
                self.env["meta.webhook.subscription"].create(values)
        delivery = self.create_delivery(self.leadgen_envelope())
        with trap_jobs():
            internal = delivery.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )
            internal._fanout_once()
            internal._fanout_once()
        self.assertEqual(len(delivery.dispatch_ids), 1)

    def test_fanout_uses_locked_terminal_state_instead_of_stale_cache(self):
        self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.page.id,
                "consumer_key": "marketing.lead_ads",
                "object_type": "page",
                "field_name": "leadgen",
            }
        )
        delivery = self.create_delivery(self.leadgen_envelope())
        internal = delivery.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        internal.write({"state": "processing", "attempts": 2})
        self.assertEqual(delivery.state, "processing")
        delivery.flush_recordset(["state", "attempts"])
        self.env.cr.execute(
            "UPDATE meta_webhook_delivery SET state = 'dead' WHERE id = %s",
            [delivery.id],
        )

        with trap_jobs() as jobs:
            self.assertFalse(internal._fanout_once())

        jobs.assert_jobs_count(0)
        delivery.invalidate_recordset(["state", "attempts"])
        self.assertEqual(delivery.state, "dead")
        self.assertEqual(delivery.attempts, 2)
        self.assertFalse(delivery.dispatch_ids)

    def test_dispatch_job_revalidates_terminal_state_before_consumer(self):
        self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.page.id,
                "consumer_key": "marketing.lead_ads",
                "object_type": "page",
                "field_name": "leadgen",
            }
        )
        delivery = self.create_delivery(self.leadgen_envelope())
        with trap_jobs():
            delivery.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )._fanout_once()
        dispatch = delivery.dispatch_ids
        internal = dispatch.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        internal.write(
            {
                "state": "processing",
                "attempts": 3,
                "queue_job_uuid": self.DISPATCH_JOB_UUID,
            }
        )
        self.assertEqual(dispatch.state, "processing")
        dispatch.flush_recordset(["state", "attempts", "queue_job_uuid"])
        self.env.cr.execute(
            "UPDATE meta_webhook_dispatch SET state = 'done' WHERE id = %s",
            [dispatch.id],
        )

        self.assertFalse(
            internal.with_context(job_uuid=self.DISPATCH_JOB_UUID)._job_process()
        )

        dispatch.invalidate_recordset(["state", "attempts"])
        self.assertEqual(dispatch.state, "done")
        self.assertEqual(dispatch.attempts, 3)

    def test_unexpected_consumer_error_is_not_retained_by_retry(self):
        delivery = self.create_delivery(self.leadgen_envelope())
        internal = delivery.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        internal.write({"queue_job_uuid": self.DELIVERY_JOB_UUID})
        secret = "provider-private-value-must-not-survive"
        delivery_class = type(delivery)

        with patch.object(
            delivery_class,
            "_fanout_once",
            autospec=True,
            side_effect=ValueError(secret),
        ), self.assertRaises(RetryableJobError) as caught:
            internal.with_context(job_uuid=self.DELIVERY_JOB_UUID)._job_fanout()

        self.assertNotIn(secret, str(caught.exception))
        self.assertIsNone(caught.exception.__context__)

    def test_fanout_preserves_transaction_retry_at_the_attempt_ceiling(self):
        delivery = self.create_delivery(self.leadgen_envelope())
        internal = delivery.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN,
            job_uuid=self.DELIVERY_JOB_UUID,
        )
        internal.write({"queue_job_uuid": self.DELIVERY_JOB_UUID, "attempts": 7})
        error = SerializationFailure("synthetic concurrent update")
        with patch.object(
            type(delivery), "_fanout_once", side_effect=error
        ), self.assertRaises(RetryableJobError) as caught:
            internal._job_fanout()
        self.assertNotIn(str(error), str(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertNotEqual(delivery.state, "dead")

    def test_dispatch_preserves_transaction_retry_at_the_attempt_ceiling(self):
        self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.page.id,
                "consumer_key": "marketing.lead_ads",
                "object_type": "page",
                "field_name": "leadgen",
            }
        )
        delivery = self.create_delivery(self.leadgen_envelope())
        with trap_jobs():
            delivery.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )._fanout_once()
        dispatch = delivery.dispatch_ids.ensure_one()
        internal = dispatch.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN,
            job_uuid=self.DISPATCH_JOB_UUID,
        )
        internal.write({"queue_job_uuid": self.DISPATCH_JOB_UUID, "attempts": 7})
        error = SerializationFailure("synthetic concurrent update")
        with patch.object(
            type(self.env["meta.webhook.dispatcher"]),
            "_dispatch_consumer",
            side_effect=error,
        ), self.assertRaises(RetryableJobError) as caught:
            internal._job_process()
        self.assertNotIn(str(error), str(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertNotEqual(dispatch.state, "dead")

    def _database_retry_records(self):
        self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.page.id,
                "consumer_key": "marketing.lead_ads",
                "object_type": "page",
                "field_name": "leadgen",
            }
        )
        delivery = self.create_delivery(self.leadgen_envelope())
        delivery = delivery.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        with trap_jobs():
            delivery._fanout_once()
        dispatch = (
            delivery.dispatch_ids.ensure_one()
            .sudo()
            .with_context(meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN)
        )
        delivery.write({"state": "pending", "attempts": 0})
        dispatch.write({"state": "pending", "attempts": 0})
        return delivery, dispatch

    def test_database_failure_budget_covers_processing_and_initial_lock(self):
        delivery, dispatch = self._database_retry_records()
        cases = (
            (delivery, "_job_fanout", type(delivery), "_fanout_once"),
            (delivery, "_job_fanout", type(delivery), "_lock_for_fanout"),
            (
                dispatch,
                "_job_process",
                type(self.env["meta.webhook.dispatcher"]),
                "_dispatch_consumer",
            ),
            (dispatch, "_job_process", type(dispatch), "_lock_for_processing"),
        )
        for record, entrypoint, target, method in cases:
            with self.subTest(model=record._name, failure=method):
                job = Job(getattr(record, entrypoint), max_retries=8)
                record.write({"queue_job_uuid": job.uuid, "attempts": 0})
                job.store()
                job.retry = 6
                with patch.object(
                    target, method, side_effect=SerializationFailure("private SQL")
                ) as failure:
                    with self.assertRaises(RetryableJobError) as retry:
                        job.perform()
                    self.assertEqual(job.retry, 7)
                    self.assertNotIn("private SQL", str(retry.exception))
                    with self.assertRaises(FailedJobError):
                        job.perform()
                    self.assertEqual(job.retry, 8)
                    self.assertEqual(failure.call_count, 2)
                # A rolled-back DB transaction does not consume application
                # delivery attempts or falsely classify the consumer as dead.
                self.assertEqual(record.attempts, 0)
                self.assertEqual(record.state, "pending")

                permanent = Job(getattr(record, entrypoint), max_retries=8)
                record.write({"queue_job_uuid": permanent.uuid})
                with patch.object(
                    target, method, side_effect=OperationalError("unclassified")
                ), self.assertRaises(OperationalError):
                    permanent.perform()
                self.assertEqual(permanent.retry, 1)

    def test_failed_queue_job_requires_explicit_requeue(self):
        delivery, dispatch = self._database_retry_records()
        for record, method in (
            (delivery, "_job_fanout"),
            (dispatch, "_job_process"),
        ):
            job = Job(
                getattr(record, method),
                max_retries=8,
                identity_key=record._identity_key(),
            )
            job.retry = 8
            job.set_failed()
            job.store()
            record.write({"queue_job_uuid": job.uuid})

        with trap_jobs() as trap, patch.object(
            fields.Datetime,
            "now",
            return_value=fields.Datetime.now() + timedelta(minutes=1),
        ):
            delivery._enqueue()
            dispatch._enqueue()
            recovered = delivery._cron_recover_orphaned_jobs(grace_seconds=0)
            self.assertEqual(recovered, 0)
            trap.assert_jobs_count(0)

        with trap_jobs() as trap:
            delivery.action_requeue()
            dispatch.action_requeue()
            trap.assert_jobs_count(2)
            trap.assert_enqueued_job(
                delivery._job_fanout, properties={"max_retries": 8}
            )
            trap.assert_enqueued_job(
                dispatch._job_process, properties={"max_retries": 8}
            )

    def test_missing_and_cancelled_jobs_remain_recoverable(self):
        delivery, dispatch = self._database_retry_records()
        # Delivery lost its queue row; dispatch was cancelled before completion.
        delivery.write({"queue_job_uuid": self.DELIVERY_JOB_UUID})
        job = Job(
            dispatch._job_process,
            max_retries=8,
            identity_key=dispatch._identity_key(),
        )
        job.set_cancelled()
        job.store()
        dispatch.write({"queue_job_uuid": job.uuid})
        with trap_jobs() as trap, patch.object(
            fields.Datetime,
            "now",
            return_value=fields.Datetime.now() + timedelta(minutes=1),
        ):
            self.assertEqual(delivery._cron_recover_orphaned_jobs(grace_seconds=0), 2)
            trap.assert_jobs_count(2)
            trap.assert_enqueued_job(
                delivery._job_fanout, properties={"max_retries": 8}
            )
            trap.assert_enqueued_job(
                dispatch._job_process, properties={"max_retries": 8}
            )

    def test_consumer_retry_error_is_sanitized_and_bounded(self):
        self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.page.id,
                "consumer_key": "marketing.lead_ads",
                "object_type": "page",
                "field_name": "leadgen",
            }
        )
        delivery = self.create_delivery(self.leadgen_envelope())
        with trap_jobs():
            delivery.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )._fanout_once()
        dispatch = delivery.dispatch_ids.ensure_one()
        internal = dispatch.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        internal.write({"queue_job_uuid": self.DISPATCH_JOB_UUID})
        secret = "provider-private-retry-value"
        dispatcher_class = type(self.env["meta.webhook.dispatcher"])

        with patch.object(
            dispatcher_class,
            "_dispatch_consumer",
            autospec=True,
            side_effect=RetryableJobError(secret, seconds=10**6),
        ), self.assertRaises(RetryableJobError) as caught:
            internal.with_context(job_uuid=self.DISPATCH_JOB_UUID)._job_process()

        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(caught.exception.seconds, 86_400)
        self.assertIsNone(caught.exception.__context__)

    def test_fanout_retry_error_is_sanitized_and_bounded(self):
        delivery = self.create_delivery(self.leadgen_envelope())
        internal = delivery.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        internal.write({"queue_job_uuid": self.DELIVERY_JOB_UUID})
        secret = "provider-private-fanout-retry-value"
        delivery_class = type(delivery)

        with patch.object(
            delivery_class,
            "_fanout_once",
            autospec=True,
            side_effect=RetryableJobError(secret, seconds=10**6),
        ), self.assertRaises(RetryableJobError) as caught:
            internal.with_context(job_uuid=self.DELIVERY_JOB_UUID)._job_fanout()

        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(caught.exception.seconds, 86_400)
        self.assertIsNone(caught.exception.__context__)

    def test_requeue_uses_locked_database_state_not_stale_cache(self):
        delivery = self.create_delivery(self.leadgen_envelope())
        internal_delivery = delivery.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        internal_delivery.write({"state": "dead", "queue_job_uuid": False})
        self.assertEqual(delivery.state, "dead")
        delivery.flush_recordset(["state"])
        self.env.cr.execute(
            "UPDATE meta_webhook_delivery SET state = 'dispatched' WHERE id = %s",
            [delivery.id],
        )

        with self.assertRaises(ValidationError):
            delivery.action_requeue()

        delivery.invalidate_recordset(["state"])
        self.assertEqual(delivery.state, "dispatched")

    def test_dispatch_policy_locks_endpoint_before_page(self):
        self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.page.id,
                "consumer_key": "marketing.lead_ads",
                "object_type": "page",
                "field_name": "leadgen",
            }
        )
        delivery = self.create_delivery(self.leadgen_envelope())
        with trap_jobs():
            delivery.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )._fanout_once()
        dispatch = delivery.dispatch_ids.ensure_one()
        cursor_class = type(self.env.cr)
        original_execute = cursor_class.execute
        locks = []

        def traced_execute(cursor, query, *args, **kwargs):
            normalized = " ".join(str(query).lower().split())
            if "for share" in normalized:
                for table in (
                    "meta_webhook_endpoint",
                    "meta_api_app",
                    "meta_webhook_page",
                ):
                    if "from %s" % table in normalized:
                        locks.append(table)
            return original_execute(cursor, query, *args, **kwargs)

        with patch.object(cursor_class, "execute", traced_execute):
            self.assertTrue(dispatch._current_policy_allows_dispatch())

        self.assertEqual(
            locks[:3],
            ["meta_webhook_endpoint", "meta_api_app", "meta_webhook_page"],
        )

    def test_archived_asset_stales_a_pending_dispatch(self):
        self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.page.id,
                "consumer_key": "marketing.lead_ads",
                "object_type": "page",
                "field_name": "leadgen",
            }
        )
        delivery = self.create_delivery(self.leadgen_envelope())
        with trap_jobs():
            delivery.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )._fanout_once()
        dispatch = delivery.dispatch_ids.ensure_one()
        self.page.asset_ids.ensure_one().write({"active": False})
        internal_dispatch = dispatch.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        internal_dispatch.write({"queue_job_uuid": self.DISPATCH_JOB_UUID})
        dispatcher_class = type(self.env["meta.webhook.dispatcher"])

        with patch.object(
            dispatcher_class, "_dispatch_consumer", autospec=True
        ) as consumer:
            self.assertFalse(
                internal_dispatch.with_context(
                    job_uuid=self.DISPATCH_JOB_UUID
                )._job_process()
            )

        consumer.assert_not_called()
        self.assertEqual(dispatch.state, "stale")

    def test_workers_require_present_and_matching_persisted_job_uuid(self):
        delivery = self.create_delivery(self.leadgen_envelope())
        internal_delivery = delivery.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        self.assertFalse(internal_delivery._lock_for_fanout(self.DELIVERY_JOB_UUID))
        internal_delivery.write({"queue_job_uuid": self.DISPATCH_JOB_UUID})
        self.assertFalse(internal_delivery._lock_for_fanout(self.DELIVERY_JOB_UUID))

        self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.page.id,
                "consumer_key": "marketing.lead_ads",
                "object_type": "page",
                "field_name": "leadgen",
            }
        )
        with trap_jobs():
            internal_delivery._fanout_once()
        dispatch = delivery.dispatch_ids.ensure_one()
        internal_dispatch = dispatch.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        internal_dispatch.write({"queue_job_uuid": False})
        self.assertFalse(internal_dispatch._lock_for_processing(self.DISPATCH_JOB_UUID))
        internal_dispatch.write({"queue_job_uuid": self.DELIVERY_JOB_UUID})
        self.assertFalse(internal_dispatch._lock_for_processing(self.DISPATCH_JOB_UUID))

    def test_active_job_lookup_requires_canonical_identity_key(self):
        self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.page.id,
                "consumer_key": "marketing.lead_ads",
                "object_type": "page",
                "field_name": "leadgen",
            }
        )
        delivery = self.create_delivery(self.leadgen_envelope())
        with trap_jobs():
            delivery.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )._fanout_once()
        dispatch = delivery.dispatch_ids.ensure_one()
        queue_jobs = self.env["queue.job"]
        queue_job_class = type(queue_jobs)

        for record, pointer in (
            (delivery, self.DELIVERY_JOB_UUID),
            (dispatch, self.DISPATCH_JOB_UUID),
        ):
            record.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            ).write({"queue_job_uuid": pointer})
            with self.subTest(model=record._name), patch.object(
                queue_job_class,
                "search",
                autospec=True,
                return_value=queue_jobs,
            ) as search:
                self.assertFalse(record._active_job())

            search.assert_called_once()
            domain = search.call_args.args[1]
            self.assertIn(("identity_key", "=", record._identity_key()), domain)
            self.assertFalse(any(term[0] == "uuid" for term in domain))

    def test_asset_archive_uses_global_policy_lock_order(self):
        asset = self.page.asset_ids.ensure_one()
        cursor_class = type(self.env.cr)
        original_execute = cursor_class.execute
        locks = []

        def traced_execute(cursor, query, *args, **kwargs):
            normalized = " ".join(str(query).lower().split())
            if "for update" in normalized:
                for table in (
                    "meta_webhook_endpoint",
                    "meta_webhook_page",
                    "meta_webhook_asset",
                ):
                    if "from %s" % table in normalized:
                        locks.append(table)
            return original_execute(cursor, query, *args, **kwargs)

        with patch.object(cursor_class, "execute", traced_execute):
            asset.write({"active": False})

        self.assertEqual(
            locks[:3],
            [
                "meta_webhook_endpoint",
                "meta_webhook_page",
                "meta_webhook_asset",
            ],
        )

    def test_delivery_and_item_evidence_are_immutable(self):
        delivery = self.create_delivery(self.leadgen_envelope())
        with self.assertRaises(AccessError):
            delivery.write({"object_type": "instagram"})
        with self.assertRaises(AccessError):
            delivery.item_ids.write({"event_field": "messages"})
        with self.assertRaises(AccessError):
            delivery.unlink()

    def test_requeue_rpc_obeys_active_company_record_rules(self):
        other_company = self.env["res.company"].create(
            {"name": "Meta webhook isolated company"}
        )
        other_app = (
            self.env["meta.api.app"]
            .with_company(other_company)
            .create(
                {
                    "name": "Isolated webhook App",
                    "company_id": other_company.id,
                    "external_app_id": "100000000000099",
                    "credential_backend": "environment",
                    "app_secret_ref": self.APP_SECRET_REF,
                }
            )
        )
        other_endpoint = (
            self.env["meta.webhook.endpoint"]
            .with_company(other_company)
            .create(
                {
                    "name": "Isolated webhook endpoint",
                    "company_id": other_company.id,
                    "app_id": other_app.id,
                    "credential_backend": "environment",
                    "verify_token_ref": self.VERIFY_TOKEN_REF,
                }
            )
        )
        other_page = (
            self.env["meta.webhook.page"]
            .with_company(other_company)
            .create(
                {
                    "name": "Isolated webhook Page",
                    "endpoint_id": other_endpoint.id,
                    "external_page_id": "100000000000199",
                    "credential_backend": "environment",
                    "access_token_ref": self.PAGE_TOKEN_REF,
                }
            )
        )
        internal = {
            "meta_webhook_internal": META_WEBHOOK_INTERNAL_TOKEN,
        }
        other_delivery = (
            self.env["meta.webhook.delivery"]
            .with_company(other_company)
            .sudo()
            .with_context(**internal)
            .create(
                {
                    "endpoint_id": other_endpoint.id,
                    "endpoint_revision": other_endpoint.revision,
                    "app_revision": other_app.revision,
                    "content_sha256": "a" * 64,
                    "body_size_bytes": 2,
                    "object_type": "page",
                    "graph_version": other_app.graph_version,
                    "sanitized_envelope_json": {"object": "page", "entry": []},
                }
            )
        )
        other_item = (
            self.env["meta.webhook.item"]
            .with_company(other_company)
            .sudo()
            .with_context(**internal)
            .create(
                {
                    "delivery_id": other_delivery.id,
                    "sequence": 0,
                    "item_key": "entry:0:messaging:0",
                    "kind": "unknown",
                    "object_type": "page",
                    "event_field": "messages",
                    "target_asset_id": other_page.external_page_id,
                    "occurrence_ref": "isolated:0",
                    "payload_json": {"safe": True},
                    "event_sha256": "b" * 64,
                }
            )
        )
        other_dispatch = (
            self.env["meta.webhook.dispatch"]
            .with_company(other_company)
            .sudo()
            .with_context(**internal)
            .create(
                {
                    "item_id": other_item.id,
                    "page_id": other_page.id,
                    "page_revision": other_page.revision,
                    "consumer_key": "isolated.consumer",
                }
            )
        )
        administrator = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Meta webhook scoped administrator",
                    "login": "meta-webhook-scoped-administrator-%s" % other_company.id,
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [(6, 0, self.env.ref("base.group_system").ids)],
                }
            )
        )

        with self.assertRaises(AccessError):
            self.env["meta.webhook.delivery"].with_user(administrator).browse(
                other_delivery.id
            ).action_requeue()
        with self.assertRaises(AccessError):
            self.env["meta.webhook.dispatch"].with_user(administrator).browse(
                other_dispatch.id
            ).action_requeue()

    def test_configuration_noop_does_not_advance_revisions(self):
        endpoint_revision = self.endpoint.revision
        page_revision = self.page.revision
        self.endpoint.write({"verify_token_ref": self.VERIFY_TOKEN_REF})
        self.page.write({"access_token_ref": self.PAGE_TOKEN_REF})
        self.assertEqual(self.endpoint.revision, endpoint_revision)
        self.assertEqual(self.page.revision, page_revision)

    def test_configuration_noop_uses_locked_database_not_stale_cache(self):
        alternate_verify_ref = "ODOO_META_WEBHOOK_ALTERNATE_VERIFY_TOKEN"
        alternate_page_ref = "ODOO_META_WEBHOOK_ALTERNATE_PAGE_TOKEN"
        self.assertEqual(self.endpoint.verify_token_ref, self.VERIFY_TOKEN_REF)
        self.assertEqual(self.page.access_token_ref, self.PAGE_TOKEN_REF)
        self.endpoint.flush_recordset(["verify_token_ref", "revision"])
        self.page.flush_recordset(["access_token_ref", "revision"])
        self.env.cr.execute(
            "UPDATE meta_webhook_endpoint "
            "SET verify_token_ref = %s, revision = revision + 1 WHERE id = %s",
            [alternate_verify_ref, self.endpoint.id],
        )
        self.env.cr.execute(
            "UPDATE meta_webhook_page "
            "SET access_token_ref = %s, revision = revision + 1 WHERE id = %s",
            [alternate_page_ref, self.page.id],
        )
        self.env.cr.execute(
            "SELECT revision FROM meta_webhook_endpoint WHERE id = %s",
            [self.endpoint.id],
        )
        endpoint_revision = self.env.cr.fetchone()[0]
        self.env.cr.execute(
            "SELECT revision FROM meta_webhook_page WHERE id = %s",
            [self.page.id],
        )
        page_revision = self.env.cr.fetchone()[0]

        self.endpoint.write({"verify_token_ref": alternate_verify_ref})
        self.page.write({"access_token_ref": alternate_page_ref})

        self.endpoint.invalidate_recordset()
        self.page.invalidate_recordset()
        self.assertEqual(self.endpoint.revision, endpoint_revision)
        self.assertEqual(self.page.revision, page_revision)

    def test_page_internal_runtime_resolves_fenced_app_and_token_in_memory(self):
        runtime, page_token, page_revision = self.page.with_context(
            meta_webhook_runtime=META_WEBHOOK_RUNTIME_TOKEN
        )._resolve_graph_runtime(
            expected_page_revision=self.page.revision,
            expected_app_revision=self.app.revision,
        )

        self.assertEqual(runtime.external_app_id, self.app.external_app_id)
        self.assertEqual(runtime.revision, self.app.revision)
        self.assertEqual(page_token, self.PAGE_TOKEN)
        self.assertEqual(page_revision, self.page.revision)
        self.assertEqual(self.page.access_token_ref, self.PAGE_TOKEN_REF)
        self.assertEqual(self.app.app_secret_ref, self.APP_SECRET_REF)

    def test_page_internal_runtime_rejects_stale_page_and_app_fences(self):
        page_revision = self.page.revision
        app_revision = self.app.revision
        self.page.write({"active": False})
        with self.assertRaises(MetaCredentialResolutionError):
            self.page.with_context(
                meta_webhook_runtime=META_WEBHOOK_RUNTIME_TOKEN
            )._resolve_graph_runtime(
                expected_page_revision=page_revision,
                expected_app_revision=app_revision,
            )

        self.page.write({"active": True})
        current_page_revision = self.page.revision
        self.app.write({"graph_version": "v25.0"})
        with self.assertRaises(MetaCredentialResolutionError):
            self.page.with_context(
                meta_webhook_runtime=META_WEBHOOK_RUNTIME_TOKEN
            )._resolve_graph_runtime(
                expected_page_revision=current_page_revision,
                expected_app_revision=app_revision,
            )

    def test_page_internal_runtime_fails_closed_when_endpoint_is_paused(self):
        self.endpoint.write({"active": False})

        with self.assertRaises(MetaCredentialResolutionError):
            self.page.with_context(
                meta_webhook_runtime=META_WEBHOOK_RUNTIME_TOKEN
            )._resolve_graph_runtime(
                expected_page_revision=self.page.revision,
                expected_app_revision=self.app.revision,
            )

    def test_page_internal_runtime_rejects_rpc_serializable_context(self):
        with self.assertRaises(AccessError):
            self.page._resolve_graph_runtime()
        with self.assertRaises(AccessError):
            self.page.with_context(meta_webhook_runtime=True)._resolve_graph_runtime()

    def test_subscription_change_advances_page_and_endpoint_fences(self):
        endpoint_revision = self.endpoint.revision
        page_revision = self.page.revision
        subscription = self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.page.id,
                "consumer_key": "marketing.lead_ads",
                "object_type": "page",
                "field_name": "leadgen",
            }
        )
        self.assertGreater(self.page.revision, page_revision)
        self.assertGreater(self.endpoint.revision, endpoint_revision)
        revisions = (self.page.revision, self.endpoint.revision)
        subscription.write({"field_name": "leadgen"})
        self.assertEqual((self.page.revision, self.endpoint.revision), revisions)

    def test_subscription_change_uses_locked_database_not_stale_cache(self):
        subscription = self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.page.id,
                "consumer_key": "contact.meta",
                "object_type": "page",
                "field_name": "messages",
            }
        )
        self.assertTrue(subscription.active)
        subscription.flush_recordset(["active"])
        self.page.flush_recordset(["revision"])
        self.endpoint.flush_recordset(["revision"])
        self.env.cr.execute(
            "UPDATE meta_webhook_subscription SET active = FALSE WHERE id = %s",
            [subscription.id],
        )
        self.env.cr.execute(
            "UPDATE meta_webhook_page SET revision = revision + 1 WHERE id = %s",
            [self.page.id],
        )
        self.env.cr.execute(
            "UPDATE meta_webhook_endpoint SET revision = revision + 1 WHERE id = %s",
            [self.endpoint.id],
        )
        self.env.cr.execute(
            "SELECT revision FROM meta_webhook_page WHERE id = %s", [self.page.id]
        )
        page_revision = self.env.cr.fetchone()[0]
        self.env.cr.execute(
            "SELECT revision FROM meta_webhook_endpoint WHERE id = %s",
            [self.endpoint.id],
        )
        endpoint_revision = self.env.cr.fetchone()[0]

        subscription.write({"active": True})

        subscription.invalidate_recordset()
        self.page.invalidate_recordset()
        self.endpoint.invalidate_recordset()
        self.assertTrue(subscription.active)
        self.assertEqual(self.page.revision, page_revision + 1)
        self.assertEqual(self.endpoint.revision, endpoint_revision + 1)

    def test_page_token_rotation_does_not_invalidate_inbound_routing_policy(self):
        subscription = self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.page.id,
                "consumer_key": "marketing.lead_ads",
                "object_type": "page",
                "field_name": "leadgen",
            }
        )
        delivery = self.create_delivery(self.leadgen_envelope())
        with trap_jobs():
            delivery.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )._fanout_once()
        dispatch = delivery.dispatch_ids
        old_revision = dispatch.page_revision
        self.page.write({"access_token_ref": "ODOO_META_WEBHOOK_ROTATED_PAGE_TOKEN"})
        self.assertNotEqual(self.page.revision, old_revision)
        self.assertEqual(self.endpoint.subscription_state, "unknown")
        self.assertTrue(subscription.active)
        self.assertTrue(dispatch._current_policy_allows_dispatch())

    def test_invalid_asset_and_subscription_contracts_fail_closed(self):
        with self.assertRaises(ValidationError):
            self.env["meta.webhook.asset"].create(
                {
                    "page_id": self.page.id,
                    "platform": "instagram",
                    "object_type": "instagram",
                    "transport": "page_linked",
                    "external_asset_id": "not-numeric",
                }
            )
        with self.assertRaises(ValidationError):
            self.env["meta.webhook.subscription"].create(
                {
                    "page_id": self.page.id,
                    "consumer_key": "invalid consumer",
                    "object_type": "page",
                    "field_name": "messages",
                }
            )

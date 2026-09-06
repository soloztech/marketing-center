import hashlib
import json
import logging
import re

from psycopg2 import OperationalError

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.meta_api_base.services.errors import (
    MetaApiError,
    MetaApiPausedError,
    MetaApiRateLimitError,
    MetaApiTransientError,
    MetaApiUncertainError,
)
from odoo.addons.meta_api_base.services.graph import (
    graph_app_access_token,
    graph_request,
)
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.retry import bounded_retry_seconds
from ..services.tokens import META_WEBHOOK_INTERNAL_TOKEN

_logger = logging.getLogger(__name__)
_ACTIVE_JOB_STATES = ("pending", "enqueued", "started", "wait_dependencies")
_ATTEMPT_CEILING = 8
_MAX_FIELDS = 64
_MAX_PAGES = 200
_MAX_SUBSCRIPTIONS = 10_000
_MAX_PROVIDER_PAGES = 10
_MAX_CURSOR_BYTES = 4 * 1024
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _canonical_digest(value):
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _job_attempt(record, job_uuid):
    job = (
        record.env["queue.job"].sudo().search([("uuid", "=", job_uuid)], limit=1)
        if job_uuid
        else record.env["queue.job"]
    )
    return max(1, (job.retry + 1) if job else 1)


def _next_after(payload, current, seen):
    paging = payload.get("paging") if isinstance(payload, dict) else None
    if paging in (None, {}):
        return ""
    if not isinstance(paging, dict):
        raise MetaApiError("Meta Graph pagination is invalid")
    next_page = paging.get("next")
    if next_page in (None, False, ""):
        # Graph returns cursors on the final page too. Only `next` proves that
        # another page exists; its URL is never followed or used as credentials.
        return ""
    if not isinstance(next_page, str):
        raise MetaApiError("Meta Graph pagination is invalid")
    cursors = paging.get("cursors")
    after = cursors.get("after") if isinstance(cursors, dict) else ""
    if after in (None, ""):
        raise MetaApiError("Meta Graph pagination is invalid")
    if not isinstance(after, str):
        raise MetaApiError("Meta Graph pagination is invalid")
    try:
        encoded = after.encode("utf-8")
    except UnicodeEncodeError:
        raise MetaApiError("Meta Graph pagination is invalid") from None
    if (
        not encoded
        or len(encoded) > _MAX_CURSOR_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in after)
        or after == current
        or after in seen
    ):
        raise MetaApiError("Meta Graph pagination did not advance")
    return after


def _app_subscription_record(value):
    if not isinstance(value, dict):
        raise MetaApiError("Meta App subscription response is invalid")
    object_type = str(value.get("object") or "").strip().lower()
    callback_url = value.get("callback_url")
    raw_fields = value.get("fields", [])
    active = value.get("active", True)
    if (
        not _FIELD_RE.fullmatch(object_type)
        or not isinstance(callback_url, str)
        or len(callback_url) > 2 * 1024
        or any(
            ord(character) < 32 or ord(character) == 127 for character in callback_url
        )
        or not isinstance(raw_fields, list)
        or len(raw_fields) > _MAX_FIELDS
        or not isinstance(active, bool)
    ):
        raise MetaApiError("Meta App subscription response is invalid")
    names = []
    for field_value in raw_fields:
        name = field_value.get("name") if isinstance(field_value, dict) else field_value
        name = str(name or "").strip().lower()
        if not _FIELD_RE.fullmatch(name):
            raise MetaApiError("Meta App subscription response is invalid")
        names.append(name)
    return {
        "object": object_type,
        "fields": tuple(sorted(set(names))),
        "callback_url": callback_url.strip(),
        "active": active,
    }


class MetaWebhookSubscriptionService(models.AbstractModel):
    _name = "meta.webhook.subscription.service"
    _description = "Meta Webhook Subscription Reconciliation Service"

    @api.model
    def _configuration(self, endpoint):
        endpoint.ensure_one()
        pages = (
            self.env["meta.webhook.page"]
            .sudo()
            .search(
                [("endpoint_id", "=", endpoint.id), ("active", "=", True)],
                order="id",
            )
        )
        if len(pages) > _MAX_PAGES:
            raise ValidationError(_("The Meta webhook endpoint has too many Pages."))
        subscriptions = (
            self.env["meta.webhook.subscription"]
            .sudo()
            .search(
                [("page_id", "in", pages.ids), ("active", "=", True)],
                order="page_id, object_type, field_name, consumer_key, id",
                limit=_MAX_SUBSCRIPTIONS + 1,
            )
        )
        if len(subscriptions) > _MAX_SUBSCRIPTIONS:
            raise ValidationError(
                _("The Meta webhook endpoint has too many subscriptions.")
            )
        app_objects = {}
        page_plans = []
        subscriptions_by_page = {}
        for subscription in subscriptions:
            subscriptions_by_page.setdefault(subscription.page_id.id, []).append(
                subscription
            )
        for page in pages:
            page_subscriptions = self.env["meta.webhook.subscription"].browse(
                [
                    subscription.id
                    for subscription in subscriptions_by_page.get(page.id, ())
                ]
            )
            # ``/{facebook-page-id}/subscribed_apps`` accepts fields belonging
            # to the Graph ``page`` object only.  Instagram fields are already
            # reconciled at App level as ``object=instagram``; sending them to
            # the Page edge makes Meta reject the whole installation with code
            # 100.  Page-linked Instagram therefore shares the credential and
            # callback, but not the Page field installation contract.
            page_fields = tuple(
                sorted(
                    set(
                        page_subscriptions.filtered(
                            lambda subscription: subscription.object_type == "page"
                        ).mapped("field_name")
                    )
                )
            )
            if len(page_fields) > _MAX_FIELDS:
                raise ValidationError(_("The Meta Page field union is too large."))
            page_plans.append(
                {
                    "page_id": page.id,
                    "page_ref": page.public_ref,
                    "revision": page.revision,
                    "required": bool(page_fields),
                    "fields": page_fields,
                }
            )
        for subscription in subscriptions:
            app_objects.setdefault(subscription.object_type, set()).add(
                subscription.field_name
            )
        normalized_objects = {
            object_type: tuple(sorted(values))
            for object_type, values in sorted(app_objects.items())
        }
        if any(len(values) > _MAX_FIELDS for values in normalized_objects.values()):
            raise ValidationError(_("The Meta App field union is too large."))
        serializable = {
            "app_objects": {
                key: list(value) for key, value in normalized_objects.items()
            },
            "pages": [dict(plan, fields=list(plan["fields"])) for plan in page_plans],
        }
        return normalized_objects, tuple(page_plans), _canonical_digest(serializable)

    @api.model
    def _identity_key(
        self,
        endpoint,
        expected_endpoint_revision,
        expected_app_revision,
        union_hash,
    ):
        return "meta_webhook:subscription:%s:%s:%s:%s" % (
            endpoint.public_ref,
            expected_endpoint_revision,
            expected_app_revision,
            union_hash[:16],
        )

    @api.model
    def _enqueue_endpoint(self, endpoint):
        endpoint.ensure_one()
        # Serialize the search/create/write sequence on the endpoint.  The OCA
        # ``identity_key`` is an application-level deduplication aid, not a
        # database uniqueness constraint, so two cron/RPC transactions could
        # otherwise both observe no active job and enqueue duplicates.  Page
        # and subscription configuration writes also lock Endpoint first,
        # making the union below a coherent snapshot.
        endpoint.flush_recordset(["active", "app_id", "revision", "queue_job_uuid"])
        self.env.cr.execute(
            "SELECT active, app_id, revision FROM meta_webhook_endpoint "
            "WHERE id = %s FOR UPDATE",
            [endpoint.id],
        )
        endpoint_row = self.env.cr.fetchone()
        if not endpoint_row:
            raise ValidationError(_("The Meta webhook endpoint no longer exists."))
        endpoint_active, app_id, endpoint_revision = endpoint_row
        endpoint.invalidate_recordset(
            ["active", "app_id", "revision", "queue_job_uuid"]
        )
        app = self.env["meta.api.app"].sudo().browse(app_id)
        app.flush_recordset(["active", "revision"])
        self.env.cr.execute(
            "SELECT active, revision FROM meta_api_app WHERE id = %s FOR SHARE",
            [app_id],
        )
        app_row = self.env.cr.fetchone()
        if not endpoint_active or not app_row or not app_row[0]:
            raise ValidationError(_("The Meta webhook endpoint or App is paused."))
        app_revision = app_row[1]
        _objects, _pages, union_hash = self._configuration(endpoint)
        identity_key = self._identity_key(
            endpoint,
            endpoint_revision,
            app_revision,
            union_hash,
        )
        active_job = (
            self.env["queue.job"]
            .sudo()
            .search(
                [
                    ("identity_key", "=", identity_key),
                    ("state", "in", _ACTIVE_JOB_STATES),
                ],
                limit=1,
            )
        )
        internal = endpoint.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        if active_job:
            internal.write({"queue_job_uuid": active_job.uuid})
            return active_job
        delayed = (
            endpoint.sudo()
            .with_delay(
                identity_key=identity_key,
                max_retries=0,
                priority=15,
                description="Meta webhook subscriptions %s" % endpoint.public_ref,
            )
            ._job_reconcile_subscriptions(
                endpoint_revision,
                app_revision,
                union_hash,
            )
        )
        internal.write({"queue_job_uuid": delayed.uuid})
        return delayed

    @api.model
    def _job_reconcile_endpoint(
        self,
        endpoint,
        *,
        expected_endpoint_revision,
        expected_app_revision,
        expected_union_hash,
    ):
        endpoint.ensure_one()
        job_uuid = self.env.context.get("job_uuid")
        # Queue identity keys reduce duplicate scheduling, but they are not a
        # database uniqueness guarantee.  Serialize workers for this endpoint
        # before any remote I/O.  A duplicate worker that was already queued
        # wakes after the owner commits, observes the cleared/replaced UUID and
        # exits without calling Meta.  The public webhook/challenge path does
        # not use this advisory namespace, so inbound admission remains live
        # while reconciliation is waiting on Graph.
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(" "hashtextextended(%s, 0))",
            ["meta_webhook_subscription_reconcile:%s" % endpoint.id],
        )
        endpoint.flush_recordset(["queue_job_uuid"])
        self.env.cr.execute(
            "SELECT queue_job_uuid FROM meta_webhook_endpoint "
            # Keep the exact job ownership stable while the reconciliation talks
            # to Meta, but do not exclude the shared reads used by the public
            # challenge/webhook admission path.  ``FOR SHARE`` still conflicts
            # with a write that would replace ``queue_job_uuid`` or change the
            # endpoint configuration; unlike ``FOR UPDATE``, it is compatible
            # with the endpoint/App snapshot taken by inbound requests.
            "WHERE id = %s FOR SHARE",
            [endpoint.id],
        )
        ownership = self.env.cr.fetchone()
        if not ownership or not ownership[0] or ownership[0] != job_uuid:
            return False
        endpoint.invalidate_recordset(["queue_job_uuid"])
        attempt = _job_attempt(endpoint, job_uuid)
        unexpected_retry = False
        try:
            with self.env.cr.savepoint():
                outcome = self._reconcile_once(
                    endpoint,
                    expected_endpoint_revision=expected_endpoint_revision,
                    expected_app_revision=expected_app_revision,
                    expected_union_hash=expected_union_hash,
                )
        except MetaApiUncertainError:
            self._project_endpoint_error(
                endpoint,
                "uncertain",
                "UncertainMutation",
                "Meta subscription outcome could not be verified.",
                expected_endpoint_revision,
                expected_app_revision,
                expected_union_hash,
            )
            return False
        except MetaApiRateLimitError as error:
            if attempt < _ATTEMPT_CEILING:
                raise RetryableJobError(
                    "Meta subscription rate limited",
                    seconds=bounded_retry_seconds(error.retry_after_seconds),
                ) from error
            self._project_endpoint_error(
                endpoint,
                "error",
                "RateLimit",
                "Meta subscription retry limit was reached.",
                expected_endpoint_revision,
                expected_app_revision,
                expected_union_hash,
            )
            return False
        except MetaApiTransientError as error:
            if attempt < _ATTEMPT_CEILING:
                raise RetryableJobError(
                    "Meta subscription temporarily failed",
                    seconds=bounded_retry_seconds(error.retry_after_seconds),
                ) from error
            self._project_endpoint_error(
                endpoint,
                "error",
                "TransientError",
                "Meta subscription retry limit was reached.",
                expected_endpoint_revision,
                expected_app_revision,
                expected_union_hash,
            )
            return False
        except MetaApiPausedError:
            self._project_endpoint_error(
                endpoint,
                "error",
                "AuthorizationUnavailable",
                "Meta subscription authorization is unavailable.",
                expected_endpoint_revision,
                expected_app_revision,
                expected_union_hash,
            )
            return False
        except MetaApiError:
            self._project_endpoint_error(
                endpoint,
                "error",
                "ProviderError",
                "Meta rejected the subscription configuration.",
                expected_endpoint_revision,
                expected_app_revision,
                expected_union_hash,
            )
            return False
        except (RetryableJobError, OperationalError):
            raise
        except Exception as error:  # queue isolation boundary
            _logger.error(
                "Unexpected Meta subscription reconciliation failure for endpoint "
                "%s: %s",
                endpoint.public_ref,
                type(error).__name__,
            )
            if attempt < _ATTEMPT_CEILING:
                unexpected_retry = True
            else:
                self._project_endpoint_error(
                    endpoint,
                    "error",
                    "UnexpectedError",
                    "Meta subscription reconciliation failed.",
                    expected_endpoint_revision,
                    expected_app_revision,
                    expected_union_hash,
                )
                return False
        if unexpected_retry:
            raise RetryableJobError("Meta subscription reconciliation failed")
        return outcome

    @api.model
    def _reconcile_once(
        self,
        endpoint,
        *,
        expected_endpoint_revision,
        expected_app_revision,
        expected_union_hash,
    ):
        desired_objects, page_plans, union_hash = self._configuration(endpoint)
        if union_hash != expected_union_hash:
            return False
        runtime, verify_token, _revision = endpoint.sudo()._locked_runtime(
            expected_revision=expected_endpoint_revision,
            expected_app_revision=expected_app_revision,
        )
        callback_url = endpoint.webhook_url or ""
        if not callback_url.startswith("https://"):
            raise MetaApiError("Meta webhook callback must use HTTPS")
        app_token = graph_app_access_token(runtime)
        observed_app = self._read_app_subscriptions(runtime, app_token)
        app_in_sync = self._app_is_in_sync(desired_objects, observed_app, callback_url)
        if not app_in_sync and desired_objects:
            for object_type, subscribed_fields in desired_objects.items():
                graph_request(
                    runtime,
                    app_token,
                    "POST",
                    "%s/subscriptions" % runtime.external_app_id,
                    data={
                        "object": object_type,
                        "callback_url": callback_url,
                        "verify_token": verify_token,
                        "fields": ",".join(subscribed_fields),
                    },
                    mutating=True,
                    max_response_bytes=64 * 1024,
                )
            try:
                observed_app = self._read_app_subscriptions(runtime, app_token)
            except (MetaApiTransientError, MetaApiRateLimitError) as error:
                raise MetaApiUncertainError(
                    "Meta App subscription readback was unavailable"
                ) from error
            app_in_sync = self._app_is_in_sync(
                desired_objects, observed_app, callback_url
            )

        page_states = []
        for plan in page_plans:
            page = self.env["meta.webhook.page"].sudo().browse(plan["page_id"])
            page_states.append(self._reconcile_page_safely(page, plan, runtime))

        if not desired_objects and observed_app:
            endpoint_state = "drift"
            error_class = "ManualRemovalRequired"
            error_message = (
                "No active fields; remote App subscriptions were not removed."
            )
        elif not app_in_sync:
            endpoint_state = "drift"
            error_class = "ReadbackMismatch"
            error_message = "Meta App subscription readback differs from the union."
        elif "uncertain" in page_states:
            endpoint_state = "uncertain"
            error_class = "PageUncertain"
            error_message = "At least one Meta Page outcome is uncertain."
        elif any(state in {"error", "drift"} for state in page_states):
            endpoint_state = "drift"
            error_class = "PageDrift"
            error_message = "At least one Meta Page installation is not in sync."
        else:
            endpoint_state = "in_sync"
            error_class = False
            error_message = False
        projected = self._project_endpoint(
            endpoint,
            endpoint_state,
            self._safe_app_observation(observed_app, callback_url),
            error_class,
            error_message,
            expected_endpoint_revision,
            expected_app_revision,
            expected_union_hash,
        )
        if projected:
            self.env["meta.webhook.dispatcher"]._after_subscription_reconcile(endpoint)
        return projected

    @api.model
    def _reconcile_page_safely(self, page, plan, runtime):
        try:
            return self._reconcile_page(page, plan, runtime)
        except (MetaApiRateLimitError, MetaApiTransientError):
            raise
        except MetaApiUncertainError:
            self._project_page(
                page,
                plan["revision"],
                "uncertain",
                "UncertainMutation",
                "Meta Page installation outcome could not be verified.",
            )
            return "uncertain"
        except MetaApiPausedError:
            self._project_page(
                page,
                plan["revision"],
                "error",
                "AuthorizationUnavailable",
                "Meta Page authorization is unavailable.",
            )
            return "error"
        except MetaApiError:
            self._project_page(
                page,
                plan["revision"],
                "error",
                "ProviderError",
                "Meta rejected the Page installation configuration.",
            )
            return "error"

    @api.model
    def _read_app_subscriptions(self, runtime, app_token):
        records = []
        path = "%s/subscriptions" % runtime.external_app_id
        after = ""
        seen = set()
        for page_number in range(1, _MAX_PROVIDER_PAGES + 1):
            params = {"limit": 100}
            if after:
                params["after"] = after
            payload = graph_request(
                runtime,
                app_token,
                "GET",
                path,
                params=params,
                max_response_bytes=128 * 1024,
            )
            data = payload.get("data")
            if not isinstance(data, list) or len(data) > 100:
                raise MetaApiError("Meta App subscription response is invalid")
            records.extend(_app_subscription_record(value) for value in data)
            if len(records) > 100:
                raise MetaApiError("Meta App subscription response is too large")
            next_after = _next_after(payload, after, seen)
            if not next_after:
                return tuple(records)
            seen.add(next_after)
            after = next_after
            if page_number == _MAX_PROVIDER_PAGES:
                raise MetaApiError("Meta App subscription page limit was exceeded")
        return tuple(records)

    @api.model
    def _app_is_in_sync(self, desired, observed, callback_url):
        if not desired:
            return not observed
        by_object = {item["object"]: item for item in observed}
        if len(by_object) != len(observed) or set(by_object) != set(desired):
            return False
        for object_type, fields_union in desired.items():
            item = by_object.get(object_type)
            if not item or not item["active"]:
                return False
            if item["callback_url"] != callback_url:
                return False
            if set(item["fields"]) != set(fields_union):
                return False
        return True

    @api.model
    def _safe_app_observation(self, observed, callback_url):
        return [
            {
                "object": item["object"],
                "fields": list(item["fields"]),
                "active": item["active"],
                "callback_matches": item["callback_url"] == callback_url,
            }
            for item in observed
        ]

    @api.model
    def _read_page_installation(self, runtime, page_token, external_page_id):
        path = "%s/subscribed_apps" % external_page_id
        after = ""
        seen = set()
        for page_number in range(1, _MAX_PROVIDER_PAGES + 1):
            params = {"limit": 100}
            if after:
                params["after"] = after
            payload = graph_request(
                runtime,
                page_token,
                "GET",
                path,
                params=params,
                max_response_bytes=128 * 1024,
            )
            data = payload.get("data")
            if not isinstance(data, list) or len(data) > 100:
                raise MetaApiError("Meta Page subscription response is invalid")
            for item in data:
                if not isinstance(item, dict):
                    raise MetaApiError("Meta Page subscription response is invalid")
                if str(item.get("id") or "") != runtime.external_app_id:
                    continue
                raw_fields = item.get("subscribed_fields", [])
                if not isinstance(raw_fields, list):
                    raise MetaApiError("Meta Page subscription response is invalid")
                fields_union = tuple(
                    sorted({str(value or "").strip().lower() for value in raw_fields})
                )
                if len(fields_union) > _MAX_FIELDS or any(
                    not _FIELD_RE.fullmatch(value) for value in fields_union
                ):
                    raise MetaApiError("Meta Page subscription response is invalid")
                return True, fields_union
            next_after = _next_after(payload, after, seen)
            if not next_after:
                return False, ()
            seen.add(next_after)
            after = next_after
            if page_number == _MAX_PROVIDER_PAGES:
                raise MetaApiError("Meta Page subscription page limit was exceeded")
        return False, ()

    @api.model
    def _reconcile_page(self, page, plan, runtime):
        page_token = page._resolved_access_token(plan["revision"])
        installed, observed = self._read_page_installation(
            runtime, page_token, page.external_page_id
        )
        desired = plan["fields"]
        if not plan["required"]:
            state = "drift" if installed else "in_sync"
            self._project_page(
                page,
                plan["revision"],
                state,
                "ManualRemovalRequired" if installed else False,
                (
                    "No active consumers; remote installation was not removed."
                    if installed
                    else False
                ),
                observed,
            )
            return state
        if installed and set(observed) == set(desired):
            self._project_page(
                page, plan["revision"], "in_sync", False, False, observed
            )
            return "in_sync"
        values = {"subscribed_fields": ",".join(desired)} if desired else {}
        graph_request(
            runtime,
            page_token,
            "POST",
            "%s/subscribed_apps" % page.external_page_id,
            data=values,
            mutating=True,
            max_response_bytes=64 * 1024,
        )
        try:
            installed, observed = self._read_page_installation(
                runtime, page_token, page.external_page_id
            )
        except (MetaApiTransientError, MetaApiRateLimitError) as error:
            raise MetaApiUncertainError(
                "Meta Page installation readback was unavailable"
            ) from error
        state = "in_sync" if installed and set(observed) == set(desired) else "drift"
        self._project_page(
            page,
            plan["revision"],
            state,
            False if state == "in_sync" else "ReadbackMismatch",
            (
                False
                if state == "in_sync"
                else "Meta Page installation readback differs from the union."
            ),
            observed,
        )
        return state

    @api.model
    def _fence_endpoint(
        self,
        endpoint,
        expected_endpoint_revision,
        expected_app_revision,
        expected_union_hash,
    ):
        if not endpoint._lock_fence(expected_endpoint_revision, expected_app_revision):
            return False
        _objects, _pages, union_hash = self._configuration(endpoint)
        return union_hash == expected_union_hash

    @api.model
    def _project_endpoint(
        self,
        endpoint,
        state,
        observed,
        error_class,
        error_message,
        expected_endpoint_revision,
        expected_app_revision,
        expected_union_hash,
    ):
        if not self._fence_endpoint(
            endpoint,
            expected_endpoint_revision,
            expected_app_revision,
            expected_union_hash,
        ):
            return False
        endpoint.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        ).write(
            {
                "subscription_state": state,
                "observed_subscriptions_json": list(observed),
                "verified_at": fields.Datetime.now(),
                "queue_job_uuid": False,
                "last_error_class": error_class,
                "last_error_message": error_message,
            }
        )
        return state == "in_sync"

    @api.model
    def _project_endpoint_error(
        self,
        endpoint,
        state,
        error_class,
        error_message,
        expected_endpoint_revision,
        expected_app_revision,
        expected_union_hash,
    ):
        return self._project_endpoint(
            endpoint,
            state,
            (),
            error_class,
            error_message,
            expected_endpoint_revision,
            expected_app_revision,
            expected_union_hash,
        )

    @api.model
    def _project_page(
        self,
        page,
        expected_revision,
        state,
        error_class,
        error_message,
        observed_fields=None,
    ):
        page.flush_recordset(["active", "revision"])
        self.env.cr.execute(
            "SELECT active, revision FROM meta_webhook_page "
            "WHERE id = %s FOR UPDATE",
            [page.id],
        )
        row = self.env.cr.fetchone()
        if not row or not row[0] or row[1] != expected_revision:
            return False
        values = {
            "subscription_state": state,
            "observed_fields_json": (
                list(observed_fields) if observed_fields is not None else False
            ),
            "verified_at": fields.Datetime.now(),
            "last_error_class": error_class,
            "last_error_message": error_message,
        }
        page.with_context(meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN).write(
            values
        )
        return True

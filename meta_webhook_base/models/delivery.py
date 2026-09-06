import datetime
import logging
import re
import uuid

from psycopg2 import IntegrityError, OperationalError

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.queue_job.exception import RetryableJobError

from ..services.retry import bounded_retry_seconds
from ..services.tokens import META_WEBHOOK_INTERNAL_TOKEN

_logger = logging.getLogger(__name__)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ITEM_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,127}$")
_ERROR_CLASS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,127}$")
_ACTIVE_JOB_STATES = ("pending", "enqueued", "started", "wait_dependencies")
_ATTEMPT_CEILING = 8
_RECOVERY_LIMIT = 100
_RECOVERY_GRACE_SECONDS = 15 * 60


def _uuid(_recordset):
    return str(uuid.uuid4())


def _internal(recordset):
    return (
        recordset.env.context.get("meta_webhook_internal")
        is META_WEBHOOK_INTERNAL_TOKEN
    )


def _job_attempt(record, job_uuid, persisted_attempts=None):
    job = (
        record.env["queue.job"].sudo().search([("uuid", "=", job_uuid)], limit=1)
        if job_uuid
        else record.env["queue.job"]
    )
    attempts = record.attempts if persisted_attempts is None else persisted_attempts
    return max(attempts + 1, (job.retry + 1) if job else 1)


class MetaWebhookDelivery(models.Model):
    _name = "meta.webhook.delivery"
    _description = "Shared Meta Webhook Delivery"
    _order = "create_date desc, id desc"
    _rec_name = "public_ref"
    _check_company_auto = True

    public_ref = fields.Char(
        required=True,
        default=_uuid,
        size=36,
        readonly=True,
        copy=False,
        index=True,
    )
    endpoint_id = fields.Many2one(
        "meta.webhook.endpoint",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    app_id = fields.Many2one(
        related="endpoint_id.app_id", store=True, readonly=True, index=True
    )
    company_id = fields.Many2one(
        related="endpoint_id.company_id", store=True, readonly=True, index=True
    )
    endpoint_revision = fields.Integer(required=True, readonly=True)
    app_revision = fields.Integer(required=True, readonly=True)
    content_sha256 = fields.Char(required=True, size=64, index=True, readonly=True)
    body_size_bytes = fields.Integer(required=True, readonly=True)
    object_type = fields.Char(required=True, size=64, index=True, readonly=True)
    graph_version = fields.Char(required=True, size=16, readonly=True)
    sanitized_envelope_json = fields.Json(required=True, readonly=True, copy=False)
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("dispatched", "Dispatched"),
            ("unrouted", "Unrouted"),
            ("stale", "Stale"),
            ("dead", "Dead"),
        ],
        required=True,
        default="pending",
        readonly=True,
        index=True,
    )
    attempts = fields.Integer(required=True, default=0, readonly=True)
    queue_job_uuid = fields.Char(readonly=True, copy=False, size=36, index=True)
    processed_at = fields.Datetime(readonly=True, copy=False, index=True)
    item_count = fields.Integer(required=True, default=0, readonly=True)
    dispatch_count = fields.Integer(required=True, default=0, readonly=True)
    last_error_class = fields.Char(readonly=True, copy=False, size=128)
    last_error_message = fields.Char(readonly=True, copy=False, size=512)
    item_ids = fields.One2many("meta.webhook.item", "delivery_id", readonly=True)
    dispatch_ids = fields.One2many(
        "meta.webhook.dispatch", "delivery_id", readonly=True
    )

    _sql_constraints = [
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The Meta webhook delivery public reference must be unique.",
        ),
        (
            "endpoint_content_unique",
            "unique(endpoint_id, content_sha256)",
            "This Meta webhook delivery was already received.",
        ),
        (
            "evidence_valid",
            "check(body_size_bytes > 0 and endpoint_revision > 0 "
            "and app_revision > 0)",
            "The Meta webhook delivery evidence is invalid.",
        ),
        (
            "counters_nonnegative",
            "check(attempts >= 0 and item_count >= 0 and dispatch_count >= 0)",
            "Meta webhook delivery counters cannot be negative.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if not _internal(self):
            raise AccessError(_("Meta webhook deliveries are created internally."))
        for values in vals_list:
            if not _SHA256_RE.fullmatch(values.get("content_sha256") or ""):
                raise ValidationError(_("The Meta webhook digest is invalid."))
            if not isinstance(values.get("sanitized_envelope_json"), dict):
                raise ValidationError(
                    _("The Meta webhook sanitized envelope must be an object.")
                )
        return super().create(vals_list)

    def write(self, values):
        if not _internal(self):
            raise AccessError(_("Meta webhook deliveries are managed internally."))
        mutable = {
            "state",
            "attempts",
            "queue_job_uuid",
            "processed_at",
            "item_count",
            "dispatch_count",
            "last_error_class",
            "last_error_message",
        }
        if set(values) - mutable:
            raise AccessError(_("Meta webhook delivery evidence is immutable."))
        return super().write(values)

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Meta webhook delivery evidence cannot be deleted."))

    def _identity_key(self):
        self.ensure_one()
        return "meta_webhook:delivery:%s" % self.public_ref

    def _active_job(self):
        self.ensure_one()
        return (
            self.env["queue.job"]
            .sudo()
            .search(
                [
                    ("identity_key", "=", self._identity_key()),
                    ("state", "in", _ACTIVE_JOB_STATES),
                ],
                limit=1,
            )
        )

    def _enqueue(self):
        for delivery in self.sorted("id"):
            delivery.flush_recordset(["state", "queue_job_uuid"])
            delivery.env.cr.execute(
                "SELECT state, queue_job_uuid FROM meta_webhook_delivery "
                "WHERE id = %s FOR UPDATE",
                [delivery.id],
            )
            row = delivery.env.cr.fetchone()
            if not row or row[0] not in {"pending", "processing"}:
                continue
            delivery.invalidate_recordset(["state", "queue_job_uuid"])
            internal = delivery.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )
            active_job = delivery._active_job()
            if active_job:
                if delivery.queue_job_uuid != active_job.uuid:
                    internal.write({"queue_job_uuid": active_job.uuid})
                continue
            delayed = (
                delivery.sudo()
                .with_delay(
                    identity_key=delivery._identity_key(),
                    max_retries=0,
                    priority=20,
                    description="Meta webhook delivery %s" % delivery.public_ref,
                )
                ._job_fanout()
            )
            internal.write({"queue_job_uuid": delayed.uuid})
        return True

    def _job_fanout(self):
        self.ensure_one()
        job_uuid = self.env.context.get("job_uuid")
        company = self.sudo().company_id
        internal = (
            self.sudo()
            .with_company(company)
            .with_context(
                allowed_company_ids=[company.id],
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN,
            )
        )
        attempt = internal._lock_for_fanout(job_uuid)
        if not attempt:
            return False
        retry_request = None
        try:
            with self.env.cr.savepoint():
                internal._fanout_once()
        except RetryableJobError as error:
            if attempt < _ATTEMPT_CEILING:
                retry_request = (
                    "Meta webhook fan-out was deferred",
                    bounded_retry_seconds(getattr(error, "seconds", None)),
                )
            else:
                internal.write(
                    {
                        "state": "dead",
                        "processed_at": fields.Datetime.now(),
                        "last_error_class": "RetryLimit",
                        "last_error_message": (
                            "Meta webhook fan-out retry limit was reached."
                        ),
                    }
                )
                _logger.warning(
                    "Meta webhook delivery retry limit reached for %s",
                    internal.public_ref,
                )
                return False
        except OperationalError:
            # Let Odoo/queue_job retry the complete database transaction. A
            # serialization conflict is not a failed provider delivery attempt.
            raise
        except Exception as error:  # queue isolation boundary
            _logger.error(
                "Unexpected Meta webhook delivery fan-out failure for %s: %s",
                internal.public_ref,
                type(error).__name__,
            )
            if attempt < _ATTEMPT_CEILING:
                retry_request = ("Meta webhook fan-out failed", None)
            else:
                internal.write(
                    {
                        "state": "dead",
                        "processed_at": fields.Datetime.now(),
                        "last_error_class": "UnexpectedError",
                        "last_error_message": "Meta webhook fan-out failed.",
                    }
                )
                return False
        if retry_request:
            # Raise outside the ``except`` suite so provider/consumer exception
            # text and chains cannot survive in queue_job diagnostics.
            raise RetryableJobError(retry_request[0], seconds=retry_request[1])
        return True

    def _lock_for_fanout(self, job_uuid):
        """Claim a delivery using its persisted state as the authority.

        The row lock is intentionally acquired before the processing savepoint and
        remains held until the queue-job transaction ends.  A duplicate worker
        therefore observes the terminal state committed by the first worker instead
        of overwriting it with ``processing`` from a stale ORM cache.
        """

        self.ensure_one()
        self.flush_recordset(["state", "attempts", "queue_job_uuid"])
        self.env.cr.execute(
            "SELECT state, attempts, queue_job_uuid "
            "FROM meta_webhook_delivery WHERE id = %s FOR UPDATE",
            [self.id],
        )
        row = self.env.cr.fetchone()
        if not row:
            return False
        state, persisted_attempts, persisted_job_uuid = row
        if not persisted_job_uuid or persisted_job_uuid != job_uuid:
            return False
        if state not in {"pending", "processing"}:
            return False
        attempt = _job_attempt(
            self,
            job_uuid,
            persisted_attempts=persisted_attempts,
        )
        self.write({"state": "processing", "attempts": attempt})
        return attempt

    def _fanout_once(self):
        self.ensure_one()
        self.flush_recordset(["state"])
        self.env.cr.execute(
            "SELECT state FROM meta_webhook_delivery WHERE id = %s FOR UPDATE",
            [self.id],
        )
        row = self.env.cr.fetchone()
        if not row or row[0] not in {"pending", "processing"}:
            return False
        endpoint = self.endpoint_id.sudo()
        if not endpoint._lock_active_policy():
            self.write(
                {
                    "state": "stale",
                    "processed_at": fields.Datetime.now(),
                    "last_error_class": "ConfigurationChanged",
                    "last_error_message": (
                        "Meta webhook endpoint or App is currently paused."
                    ),
                }
            )
            return False
        dispatches = self.env["meta.webhook.dispatch"]
        asset_model = (
            self.env["meta.webhook.asset"].sudo().with_context(active_test=False)
        )
        subscription_model = self.env["meta.webhook.subscription"].sudo()
        for item in self.item_ids.sorted("sequence"):
            asset = asset_model.search(
                [
                    ("endpoint_id", "=", endpoint.id),
                    ("object_type", "=", item.object_type),
                    ("external_asset_id", "=", item.target_asset_id),
                ],
                limit=1,
            )
            page = asset.page_id
            if not asset or not asset.active or not page.active:
                continue
            subscriptions = subscription_model.search(
                [
                    ("page_id", "=", page.id),
                    ("active", "=", True),
                    ("object_type", "=", item.object_type),
                    ("field_name", "=", item.event_field),
                ],
                order="consumer_key, id",
            )
            for consumer_key in sorted(set(subscriptions.mapped("consumer_key"))):
                dispatch = self._find_or_create_dispatch(
                    item,
                    page,
                    consumer_key,
                )
                dispatches |= dispatch
        dispatches._enqueue()
        self.write(
            {
                "state": "dispatched" if dispatches else "unrouted",
                "processed_at": fields.Datetime.now(),
                "item_count": len(self.item_ids),
                "dispatch_count": len(dispatches),
                "last_error_class": False,
                "last_error_message": False,
            }
        )
        return True

    def _find_or_create_dispatch(self, item, page, consumer_key):
        dispatch_model = (
            self.env["meta.webhook.dispatch"]
            .sudo()
            .with_context(meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN)
        )
        domain = [
            ("item_id", "=", item.id),
            ("consumer_key", "=", consumer_key),
        ]
        dispatch = dispatch_model.search(domain, limit=1)
        if dispatch:
            return dispatch
        try:
            with self.env.cr.savepoint():
                return dispatch_model.create(
                    {
                        "item_id": item.id,
                        "page_id": page.id,
                        "page_revision": page.revision,
                        "consumer_key": consumer_key,
                    }
                )
        except IntegrityError:
            dispatch = dispatch_model.search(domain, limit=1)
            if not dispatch:
                raise
            return dispatch

    def action_requeue(self):
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only system administrators can requeue deliveries."))
        self.check_access_rights("read")
        self.check_access_rule("read")
        for delivery in self.sorted("id"):
            delivery.flush_recordset(["state", "queue_job_uuid"])
            delivery.env.cr.execute(
                "SELECT state, queue_job_uuid FROM meta_webhook_delivery "
                "WHERE id = %s FOR UPDATE",
                [delivery.id],
            )
            row = delivery.env.cr.fetchone()
            if not row or row[0] not in {
                "pending",
                "processing",
                "unrouted",
                "stale",
                "dead",
            }:
                raise ValidationError(
                    _("This Meta webhook delivery cannot be requeued.")
                )
            delivery.invalidate_recordset(["state", "queue_job_uuid"])
            if delivery._active_job():
                raise ValidationError(
                    _("Wait for the active Meta webhook job before requeueing.")
                )
        internal = self.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        internal.write(
            {
                "state": "pending",
                "attempts": 0,
                "queue_job_uuid": False,
                "processed_at": False,
                "last_error_class": False,
                "last_error_message": False,
            }
        )
        return internal._enqueue()

    @api.model
    def _lock_orphaned_ids(self, cutoff, limit):
        self.flush_model(["state", "write_date"])
        self.env["queue.job"].sudo().flush_model(["identity_key", "state"])
        self.env.cr.execute(
            """
            SELECT delivery.id
              FROM meta_webhook_delivery AS delivery
             WHERE delivery.state IN ('pending', 'processing')
               AND COALESCE(delivery.write_date, delivery.create_date) <= %s
               AND NOT EXISTS (
                    SELECT 1
                      FROM queue_job AS job
                     WHERE job.state IN %s
                       AND (
                            job.identity_key =
                                'meta_webhook:delivery:' || delivery.public_ref
                       )
               )
          ORDER BY COALESCE(delivery.write_date, delivery.create_date), delivery.id
             FOR UPDATE OF delivery SKIP LOCKED
             LIMIT %s
            """,
            [cutoff, _ACTIVE_JOB_STATES, limit],
        )
        return [row[0] for row in self.env.cr.fetchall()]

    @api.model
    def _cron_recover_orphaned_jobs(
        self, limit=_RECOVERY_LIMIT, grace_seconds=_RECOVERY_GRACE_SECONDS
    ):
        limit = max(0, min(int(limit or 0), 500))
        grace_seconds = max(0, int(grace_seconds or 0))
        if not limit:
            return 0
        cutoff = fields.Datetime.now() - datetime.timedelta(seconds=grace_seconds)
        ids = self._lock_orphaned_ids(cutoff, limit)
        recovered = 0
        for delivery in self.sudo().browse(ids).exists():
            internal = delivery.with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )
            internal.write({"state": "pending", "queue_job_uuid": False})
            internal._enqueue()
            recovered += 1
        remaining = max(0, limit - recovered)
        if remaining:
            recovered += self.env["meta.webhook.dispatch"]._recover_orphaned_jobs(
                limit=remaining, grace_seconds=grace_seconds
            )
        if recovered:
            _logger.info("Recovered orphaned Meta webhook jobs: %s", recovered)
        return recovered


class MetaWebhookItem(models.Model):
    _name = "meta.webhook.item"
    _description = "Immutable Meta Webhook Item"
    _order = "delivery_id, sequence, id"
    _rec_name = "item_key"
    _check_company_auto = True

    delivery_id = fields.Many2one(
        "meta.webhook.delivery", required=True, index=True, ondelete="restrict"
    )
    company_id = fields.Many2one(
        related="delivery_id.company_id", store=True, readonly=True, index=True
    )
    sequence = fields.Integer(required=True, readonly=True)
    item_key = fields.Char(required=True, size=128, index=True, readonly=True)
    kind = fields.Selection(
        [("leadgen", "Lead Ads"), ("messaging", "Messaging"), ("unknown", "Unknown")],
        required=True,
        index=True,
        readonly=True,
    )
    object_type = fields.Char(required=True, size=64, index=True, readonly=True)
    event_field = fields.Char(required=True, size=64, index=True, readonly=True)
    target_asset_id = fields.Char(required=True, size=40, index=True, readonly=True)
    occurrence_ref = fields.Char(required=True, size=256, index=True, readonly=True)
    payload_json = fields.Json(required=True, readonly=True, copy=False)
    event_sha256 = fields.Char(required=True, size=64, index=True, readonly=True)
    dispatch_ids = fields.One2many("meta.webhook.dispatch", "item_id", readonly=True)

    _sql_constraints = [
        (
            "delivery_item_key_unique",
            "unique(delivery_id, item_key)",
            "This Meta webhook item already exists in the delivery.",
        ),
        (
            "delivery_sequence_unique",
            "unique(delivery_id, sequence)",
            "This Meta webhook sequence already exists in the delivery.",
        ),
        (
            "sequence_nonnegative",
            "check(sequence >= 0)",
            "The Meta webhook item sequence cannot be negative.",
        ),
        (
            "event_hash_sha256",
            "check(char_length(event_sha256) = 64)",
            "The Meta webhook item digest must be SHA-256.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if not _internal(self):
            raise AccessError(_("Meta webhook items are created internally."))
        for values in vals_list:
            if not _ITEM_KEY_RE.fullmatch(values.get("item_key") or ""):
                raise ValidationError(_("The Meta webhook item key is invalid."))
            if not _SHA256_RE.fullmatch(values.get("event_sha256") or ""):
                raise ValidationError(_("The Meta webhook item digest is invalid."))
            if not isinstance(values.get("payload_json"), dict):
                raise ValidationError(_("The Meta webhook item must be an object."))
        return super().create(vals_list)

    def write(self, values):  # pylint: disable=method-required-super
        raise AccessError(_("Meta webhook item evidence is immutable."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Meta webhook item evidence cannot be deleted."))


class MetaWebhookDispatch(models.Model):
    _name = "meta.webhook.dispatch"
    _description = "Meta Webhook Consumer Dispatch"
    _order = "create_date desc, id desc"
    _check_company_auto = True

    item_id = fields.Many2one(
        "meta.webhook.item", required=True, index=True, ondelete="restrict"
    )
    delivery_id = fields.Many2one(
        related="item_id.delivery_id", store=True, readonly=True, index=True
    )
    company_id = fields.Many2one(
        related="item_id.company_id", store=True, readonly=True, index=True
    )
    page_id = fields.Many2one(
        "meta.webhook.page",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    page_revision = fields.Integer(required=True, readonly=True)
    consumer_key = fields.Char(required=True, size=128, index=True, readonly=True)
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("done", "Done"),
            ("unrouted", "Unrouted"),
            ("stale", "Stale"),
            ("dead", "Dead"),
        ],
        required=True,
        default="pending",
        readonly=True,
        index=True,
    )
    attempts = fields.Integer(required=True, default=0, readonly=True)
    queue_job_uuid = fields.Char(readonly=True, copy=False, size=36, index=True)
    processed_at = fields.Datetime(readonly=True, copy=False, index=True)
    result_ref = fields.Char(readonly=True, copy=False, size=512)
    last_error_class = fields.Char(readonly=True, copy=False, size=128)
    last_error_message = fields.Char(readonly=True, copy=False, size=512)

    _sql_constraints = [
        (
            "item_consumer_unique",
            "unique(item_id, consumer_key)",
            "This Meta webhook item is already dispatched to the consumer.",
        ),
        (
            "dispatch_counters_valid",
            "check(attempts >= 0 and page_revision > 0)",
            "Meta webhook dispatch counters are invalid.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if not _internal(self):
            raise AccessError(_("Meta webhook dispatches are created internally."))
        return super().create(vals_list)

    def write(self, values):
        if not _internal(self):
            raise AccessError(_("Meta webhook dispatches are managed internally."))
        mutable = {
            "state",
            "attempts",
            "queue_job_uuid",
            "processed_at",
            "result_ref",
            "last_error_class",
            "last_error_message",
        }
        if set(values) - mutable:
            raise AccessError(_("Meta webhook dispatch identity is immutable."))
        return super().write(values)

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Meta webhook dispatches cannot be deleted."))

    def _identity_key(self):
        self.ensure_one()
        return "meta_webhook:dispatch:%s:%s" % (
            self.item_id.id,
            self.consumer_key,
        )

    def _active_job(self):
        self.ensure_one()
        return (
            self.env["queue.job"]
            .sudo()
            .search(
                [
                    ("identity_key", "=", self._identity_key()),
                    ("state", "in", _ACTIVE_JOB_STATES),
                ],
                limit=1,
            )
        )

    def _enqueue(self):
        for dispatch in self.sorted("id"):
            dispatch.flush_recordset(["state", "queue_job_uuid"])
            dispatch.env.cr.execute(
                "SELECT state, queue_job_uuid FROM meta_webhook_dispatch "
                "WHERE id = %s FOR UPDATE",
                [dispatch.id],
            )
            row = dispatch.env.cr.fetchone()
            if not row or row[0] != "pending":
                continue
            dispatch.invalidate_recordset(["state", "queue_job_uuid"])
            internal = dispatch.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )
            active_job = dispatch._active_job()
            if active_job:
                if dispatch.queue_job_uuid != active_job.uuid:
                    internal.write({"queue_job_uuid": active_job.uuid})
                continue
            delayed = (
                dispatch.sudo()
                .with_delay(
                    identity_key=dispatch._identity_key(),
                    max_retries=0,
                    priority=25,
                    description="Meta webhook dispatch %s" % dispatch.id,
                )
                ._job_process()
            )
            internal.write({"queue_job_uuid": delayed.uuid})
        return True

    def _job_process(self):
        self.ensure_one()
        job_uuid = self.env.context.get("job_uuid")
        company = self.sudo().company_id
        internal = (
            self.sudo()
            .with_company(company)
            .with_context(
                allowed_company_ids=[company.id],
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN,
            )
        )
        attempt = internal._lock_for_processing(job_uuid)
        if not attempt:
            return False
        retry_request = None
        try:
            with self.env.cr.savepoint():
                if not internal._current_policy_allows_dispatch():
                    internal.write(
                        {
                            "state": "stale",
                            "processed_at": fields.Datetime.now(),
                            "last_error_class": "RoutingPolicyChanged",
                            "last_error_message": (
                                "Meta webhook routing policy is no longer active."
                            ),
                        }
                    )
                    return False
                dispatcher = internal.env["meta.webhook.dispatcher"]
                result = dispatcher._dispatch_consumer(internal)
                if result:
                    result = dispatcher._validated_dispatch_result(result)
        except RetryableJobError as error:
            if attempt < _ATTEMPT_CEILING:
                retry_request = (
                    "Meta webhook consumer was deferred",
                    bounded_retry_seconds(getattr(error, "seconds", None)),
                )
            else:
                error_class = internal.env[
                    "meta.webhook.dispatcher"
                ]._dispatch_retry_limit_error_class(internal, error)
                if not isinstance(error_class, str) or not _ERROR_CLASS_RE.fullmatch(
                    error_class
                ):
                    error_class = "RetryLimit"
                internal.write(
                    {
                        "state": "dead",
                        "processed_at": fields.Datetime.now(),
                        "last_error_class": error_class,
                        "last_error_message": (
                            "Meta webhook consumer retry limit was reached."
                        ),
                    }
                )
                _logger.warning(
                    "Meta webhook dispatch retry limit reached for %s", internal.id
                )
                return False
        except OperationalError:
            raise
        except Exception as error:  # queue isolation boundary
            _logger.error(
                "Unexpected Meta webhook consumer failure for dispatch %s: %s",
                internal.id,
                type(error).__name__,
            )
            if attempt < _ATTEMPT_CEILING:
                retry_request = ("Meta webhook consumer failed", None)
            else:
                internal.write(
                    {
                        "state": "dead",
                        "processed_at": fields.Datetime.now(),
                        "last_error_class": "UnexpectedError",
                        "last_error_message": "Meta webhook consumer failed.",
                    }
                )
                return False
        if retry_request:
            raise RetryableJobError(retry_request[0], seconds=retry_request[1])
        if not result:
            internal.write(
                {
                    "state": "unrouted",
                    "processed_at": fields.Datetime.now(),
                    "last_error_class": False,
                    "last_error_message": False,
                }
            )
            return False
        internal.write(
            {
                "state": "done",
                "processed_at": fields.Datetime.now(),
                "result_ref": result.get("result_ref") or False,
                "last_error_class": False,
                "last_error_message": False,
            }
        )
        return True

    def _lock_for_processing(self, job_uuid):
        """Serialize a consumer effect and revalidate state under ``FOR UPDATE``.

        The lock spans policy validation, the consumer call and the terminal state
        write.  This makes a duplicate queue worker wait and then stop when the first
        worker has already completed the dispatch.
        """

        self.ensure_one()
        self.flush_recordset(["state", "attempts", "queue_job_uuid"])
        self.env.cr.execute(
            "SELECT state, attempts, queue_job_uuid "
            "FROM meta_webhook_dispatch WHERE id = %s FOR UPDATE",
            [self.id],
        )
        row = self.env.cr.fetchone()
        if not row:
            return False
        state, persisted_attempts, persisted_job_uuid = row
        if not persisted_job_uuid or persisted_job_uuid != job_uuid:
            return False
        if state not in {"pending", "processing"}:
            return False
        attempt = _job_attempt(
            self,
            job_uuid,
            persisted_attempts=persisted_attempts,
        )
        self.write({"state": "processing", "attempts": attempt})
        return attempt

    def _current_policy_allows_dispatch(self):
        self.ensure_one()
        page = self.page_id
        # ``endpoint_id`` is immutable. Resolve it before taking locks, then use
        # the global Endpoint -> App -> Page order shared with configuration
        # writes. The previous Page -> Endpoint order could deadlock against a
        # concurrent Page archive or credential rotation.
        endpoint = page.endpoint_id.sudo()
        if not endpoint._lock_active_policy():
            return False
        page.flush_recordset(["active", "endpoint_id"])
        self.env.cr.execute(
            "SELECT active, endpoint_id FROM meta_webhook_page "
            "WHERE id = %s FOR SHARE",
            [page.id],
        )
        row = self.env.cr.fetchone()
        if not row or not row[0] or row[1] != endpoint.id:
            return False
        asset = (
            self.env["meta.webhook.asset"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("endpoint_id", "=", endpoint.id),
                    ("page_id", "=", page.id),
                    ("object_type", "=", self.item_id.object_type),
                    ("external_asset_id", "=", self.item_id.target_asset_id),
                ],
                limit=1,
            )
        )
        if not asset:
            return False
        asset.flush_recordset(["active", "page_id", "endpoint_id"])
        self.env.cr.execute(
            "SELECT active, page_id, endpoint_id FROM meta_webhook_asset "
            "WHERE id = %s FOR SHARE",
            [asset.id],
        )
        asset_row = self.env.cr.fetchone()
        if (
            not asset_row
            or not asset_row[0]
            or asset_row[1] != page.id
            or asset_row[2] != endpoint.id
        ):
            return False
        return bool(
            self.env["meta.webhook.subscription"]
            .sudo()
            .search_count(
                [
                    ("page_id", "=", page.id),
                    ("active", "=", True),
                    ("consumer_key", "=", self.consumer_key),
                    ("object_type", "=", self.item_id.object_type),
                    ("field_name", "=", self.item_id.event_field),
                ]
            )
        )

    def action_requeue(self):
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only system administrators can requeue dispatches."))
        self.check_access_rights("read")
        self.check_access_rule("read")
        for dispatch in self.sorted("id"):
            dispatch.flush_recordset(["state", "queue_job_uuid"])
            dispatch.env.cr.execute(
                "SELECT state, queue_job_uuid FROM meta_webhook_dispatch "
                "WHERE id = %s FOR UPDATE",
                [dispatch.id],
            )
            row = dispatch.env.cr.fetchone()
            if not row or row[0] not in {
                "pending",
                "processing",
                "unrouted",
                "stale",
                "dead",
            }:
                raise ValidationError(
                    _("This Meta webhook dispatch cannot be requeued.")
                )
            dispatch.invalidate_recordset(["state", "queue_job_uuid"])
            if dispatch._active_job():
                raise ValidationError(
                    _("Wait for the active Meta webhook job before requeueing.")
                )
        internal = self.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        internal.write(
            {
                "state": "pending",
                "attempts": 0,
                "queue_job_uuid": False,
                "processed_at": False,
                "result_ref": False,
                "last_error_class": False,
                "last_error_message": False,
            }
        )
        return internal._enqueue()

    @api.model
    def _recover_orphaned_jobs(
        self, limit=_RECOVERY_LIMIT, grace_seconds=_RECOVERY_GRACE_SECONDS
    ):
        limit = max(0, min(int(limit or 0), 500))
        if not limit:
            return 0
        cutoff = fields.Datetime.now() - datetime.timedelta(
            seconds=max(0, int(grace_seconds or 0))
        )
        self.flush_model(["state", "write_date"])
        self.env["queue.job"].sudo().flush_model(["identity_key", "state"])
        self.env.cr.execute(
            """
            SELECT dispatch.id
              FROM meta_webhook_dispatch AS dispatch
             WHERE dispatch.state IN ('pending', 'processing')
               AND COALESCE(dispatch.write_date, dispatch.create_date) <= %s
               AND NOT EXISTS (
                    SELECT 1
                      FROM queue_job AS job
                     WHERE job.state IN %s
                       AND (
                            job.identity_key = 'meta_webhook:dispatch:' ||
                                dispatch.item_id::text || ':' || dispatch.consumer_key
                       )
               )
          ORDER BY COALESCE(dispatch.write_date, dispatch.create_date), dispatch.id
             FOR UPDATE OF dispatch SKIP LOCKED
             LIMIT %s
            """,
            [cutoff, _ACTIVE_JOB_STATES, limit],
        )
        ids = [row[0] for row in self.env.cr.fetchall()]
        for dispatch in self.sudo().browse(ids).exists():
            internal = dispatch.with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )
            internal.write({"state": "pending", "queue_job_uuid": False})
            internal._enqueue()
        return len(ids)

import logging
import secrets
import uuid
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.meta_api_base.services import (
    META_API_RUNTIME_CONTEXT_KEY,
    META_API_RUNTIME_TOKEN,
)
from odoo.addons.meta_api_base.services.credentials import (
    MetaCredentialResolutionError,
    resolve_secret,
    validate_secret_reference,
)

from ..services.contracts import META_WEBHOOK_REFRESH_AFTER, META_WEBHOOK_ROUTING_KEY_RE
from ..services.tokens import META_WEBHOOK_INTERNAL_TOKEN

_logger = logging.getLogger(__name__)

_RECONCILE_BATCH_LIMIT = 20
_RECONCILE_BATCH_LIMIT_MAX = 100
_SUBSCRIPTION_REFRESH_SECONDS_MIN = 5 * 60
_SUBSCRIPTION_REFRESH_SECONDS_MAX = 15 * 60
_ACTIVE_QUEUE_JOB_STATES = ("pending", "enqueued", "started", "wait_dependencies")


def _uuid(_recordset):
    return str(uuid.uuid4())


class MetaWebhookEndpoint(models.Model):
    _name = "meta.webhook.endpoint"
    _description = "Shared Meta Webhook Endpoint"
    _order = "company_id, name, id"
    _check_company_auto = True

    name = fields.Char(required=True, index=True)
    active = fields.Boolean(default=True, index=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
        ondelete="restrict",
    )
    public_ref = fields.Char(
        required=True,
        default=_uuid,
        size=36,
        readonly=True,
        copy=False,
        index=True,
    )
    app_id = fields.Many2one(
        "meta.api.app",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    routing_key = fields.Char(
        required=True,
        default=lambda self: secrets.token_urlsafe(32),
        size=128,
        readonly=True,
        copy=False,
        index=True,
        groups="base.group_system",
    )
    credential_backend = fields.Selection(
        [("environment", "Environment"), ("file", "Mounted secret file")],
        required=True,
        default="environment",
    )
    verify_token_ref = fields.Char(
        required=True,
        size=128,
        groups="base.group_system",
        help="Environment key or mounted secret filename; never the token value.",
    )
    revision = fields.Integer(
        required=True,
        default=1,
        readonly=True,
        copy=False,
        index=True,
    )
    webhook_url = fields.Char(compute="_compute_webhook_url")
    page_ids = fields.One2many("meta.webhook.page", "endpoint_id", readonly=True)
    delivery_ids = fields.One2many(
        "meta.webhook.delivery", "endpoint_id", readonly=True
    )
    subscription_state = fields.Selection(
        [
            ("unknown", "Unknown"),
            ("in_sync", "In Sync"),
            ("drift", "Drift"),
            ("error", "Error"),
            ("uncertain", "Uncertain"),
        ],
        required=True,
        default="unknown",
        readonly=True,
        index=True,
    )
    observed_subscriptions_json = fields.Json(readonly=True, copy=False)
    verified_at = fields.Datetime(readonly=True, copy=False, index=True)
    last_error_class = fields.Char(readonly=True, copy=False, size=128)
    last_error_message = fields.Char(readonly=True, copy=False, size=512)
    queue_job_uuid = fields.Char(readonly=True, copy=False, size=36, index=True)

    _sql_constraints = [
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The Meta webhook endpoint public reference must be unique.",
        ),
        (
            "routing_key_unique",
            "unique(routing_key)",
            "The Meta webhook routing key must be unique.",
        ),
        (
            "company_app_unique",
            "unique(company_id, app_id)",
            "This company already has a webhook endpoint for the Meta App.",
        ),
        (
            "revision_positive",
            "check(revision > 0)",
            "The Meta webhook endpoint revision must be positive.",
        ),
    ]

    @api.depends("routing_key")
    def _compute_webhook_url(self):
        base_url = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("web.base.url", "")
            .rstrip("/")
        )
        for endpoint in self:
            endpoint.webhook_url = (
                "%s/meta/webhook/%s" % (base_url, endpoint.routing_key)
                if base_url and endpoint.routing_key
                else False
            )

    @api.model_create_multi
    def create(self, vals_list):
        normalized = []
        managed_fields = {
            "public_ref",
            "revision",
            "routing_key",
            "subscription_state",
            "observed_subscriptions_json",
            "verified_at",
            "last_error_class",
            "last_error_message",
            "queue_job_uuid",
        }
        for incoming in vals_list:
            values = dict(incoming)
            if managed_fields.intersection(values):
                raise AccessError(
                    _("Meta webhook identity and runtime state are managed internally.")
                )
            values["verify_token_ref"] = str(
                values.get("verify_token_ref") or ""
            ).strip()
            normalized.append(values)
        endpoints = super().create(normalized)
        paused = endpoints.filtered(lambda endpoint: not endpoint.app_id.active)
        if paused:
            paused.with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            ).write(
                {
                    "subscription_state": "error",
                    "last_error_class": "AppPaused",
                    "last_error_message": "The shared Meta App is paused.",
                }
            )
        return endpoints

    def write(self, values):
        values = dict(values)
        internal = (
            self.env.context.get("meta_webhook_internal") is META_WEBHOOK_INTERNAL_TOKEN
        )
        runtime_fields = {
            "subscription_state",
            "observed_subscriptions_json",
            "verified_at",
            "last_error_class",
            "last_error_message",
            "queue_job_uuid",
        }
        if runtime_fields.intersection(values) and not internal:
            raise AccessError(
                _("Meta webhook endpoint runtime state is managed internally.")
            )
        if "public_ref" in values or "revision" in values:
            raise AccessError(
                _("Meta webhook identity and revision are managed internally.")
            )
        if "company_id" in values and any(
            endpoint.company_id.id != values["company_id"] for endpoint in self
        ):
            raise AccessError(_("A Meta webhook endpoint cannot change company."))
        if "app_id" in values and any(
            endpoint.app_id.id != values["app_id"] for endpoint in self
        ):
            raise AccessError(_("A Meta webhook endpoint cannot change App."))
        if "routing_key" in values and any(
            endpoint.routing_key != values["routing_key"] for endpoint in self
        ):
            raise AccessError(_("The Meta webhook routing key is immutable."))
        if "verify_token_ref" in values:
            values["verify_token_ref"] = str(
                values.get("verify_token_ref") or ""
            ).strip()
        revision_fields = (
            "active",
            "credential_backend",
            "verify_token_ref",
        )
        if not set(revision_fields).intersection(values):
            return super().write(values)
        # Revision fencing below uses raw SQL. Make pending ORM values visible
        # before taking the lock so SQL and the record cache cannot diverge.
        self.flush_recordset(["company_id", "app_id", "routing_key", "revision"])
        for endpoint in self.sorted("id"):
            self.env.cr.execute(
                "SELECT revision, active, credential_backend, verify_token_ref "
                "FROM meta_webhook_endpoint "
                "WHERE id = %s FOR UPDATE",
                [endpoint.id],
            )
            row = self.env.cr.fetchone()
            if not row:
                raise AccessError(_("The Meta webhook endpoint no longer exists."))
            persisted = dict(zip(revision_fields, row[1:]))
            changed = any(
                values[field_name] != persisted[field_name]
                for field_name in set(revision_fields).intersection(values)
            )
            endpoint_values = dict(values)
            if changed:
                endpoint_values["revision"] = row[0] + 1
            if changed and not internal:
                endpoint_values.update(
                    {
                        "subscription_state": "unknown",
                        "observed_subscriptions_json": False,
                        "verified_at": False,
                        "last_error_class": False,
                        "last_error_message": False,
                    }
                )
            super(MetaWebhookEndpoint, endpoint).write(endpoint_values)
            endpoint.flush_recordset(list(endpoint_values))
            endpoint.invalidate_recordset(["revision"])
        return True

    @api.constrains(
        "company_id",
        "app_id",
        "routing_key",
        "credential_backend",
        "verify_token_ref",
    )
    def _check_configuration(self):
        for endpoint in self:
            if endpoint.app_id.company_id != endpoint.company_id:
                raise ValidationError(
                    _("The Meta webhook App belongs to another company.")
                )
            if not META_WEBHOOK_ROUTING_KEY_RE.fullmatch(endpoint.routing_key or ""):
                raise ValidationError(_("The Meta webhook routing key is invalid."))
            try:
                validate_secret_reference(
                    endpoint.credential_backend,
                    endpoint.verify_token_ref,
                )
            except MetaCredentialResolutionError:
                raise ValidationError(
                    _("The Meta webhook verify-token reference is invalid.")
                ) from None

    def _locked_runtime(
        self,
        expected_revision=None,
        expected_app_revision=None,
        *,
        require_verify_token=True,
    ):
        """Return a fenced App runtime plus an optional webhook verify token.

        Signed POST admission requires only the App Secret used for HMAC.  The
        verify token is resolved for Meta's GET challenge and subscription
        mutation, but its temporary absence must not stop already-authenticated
        inbound deliveries.
        """

        self.ensure_one()
        for value in (expected_revision, expected_app_revision):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise MetaCredentialResolutionError(
                    "Meta webhook expected revision is invalid"
                )
        self.flush_recordset(
            [
                "active",
                "app_id",
                "credential_backend",
                "verify_token_ref",
                "revision",
            ]
        )
        self.env.cr.execute(
            "SELECT active, app_id, credential_backend, verify_token_ref, revision "
            "FROM meta_webhook_endpoint WHERE id = %s FOR SHARE",
            [self.id],
        )
        row = self.env.cr.fetchone()
        if not row:
            raise MetaCredentialResolutionError("Meta webhook endpoint is unavailable")
        active, app_id, backend, token_ref, revision = row
        if expected_revision is not None and revision != expected_revision:
            raise MetaCredentialResolutionError(
                "Meta webhook endpoint configuration changed"
            )
        if not active:
            raise MetaCredentialResolutionError("Meta webhook endpoint is paused")
        runtime = (
            self.env["meta.api.app"]
            .sudo()
            .browse(app_id)
            .with_context(**{META_API_RUNTIME_CONTEXT_KEY: META_API_RUNTIME_TOKEN})
            ._resolve_runtime(expected_revision=expected_app_revision)
        )
        verify_token = (
            resolve_secret(backend, token_ref) if require_verify_token else ""
        )
        return runtime, verify_token, revision

    def _lock_fence(self, expected_revision, expected_app_revision):
        """Fence an already-authenticated delivery without resolving secrets."""

        self.ensure_one()
        self.flush_recordset(["active", "app_id", "revision"])
        self.env.cr.execute(
            "SELECT active, app_id, revision FROM meta_webhook_endpoint "
            "WHERE id = %s FOR SHARE",
            [self.id],
        )
        row = self.env.cr.fetchone()
        if not row:
            return False
        active, app_id, revision = row
        if not active or revision != expected_revision:
            return False
        self.env["meta.api.app"].sudo().browse(app_id).flush_recordset(
            ["active", "revision"]
        )
        self.env.cr.execute(
            "SELECT active, revision FROM meta_api_app WHERE id = %s FOR SHARE",
            [app_id],
        )
        app_row = self.env.cr.fetchone()
        return bool(app_row and app_row[0] and app_row[1] == expected_app_revision)

    def _lock_active_policy(self):
        """Check current admission policy without resolving or fencing secrets."""

        self.ensure_one()
        self.flush_recordset(["active", "app_id"])
        self.env.cr.execute(
            "SELECT active, app_id FROM meta_webhook_endpoint "
            "WHERE id = %s FOR SHARE",
            [self.id],
        )
        row = self.env.cr.fetchone()
        if not row or not row[0]:
            return False
        self.env["meta.api.app"].sudo().browse(row[1]).flush_recordset(["active"])
        self.env.cr.execute(
            "SELECT active FROM meta_api_app WHERE id = %s FOR SHARE",
            [row[1]],
        )
        app_row = self.env.cr.fetchone()
        return bool(app_row and app_row[0])

    def _meta_subscription_configuration_changed(self):
        self.flush_recordset(["revision"])
        for endpoint in self.sorted("id"):
            self.env.cr.execute(
                "SELECT revision FROM meta_webhook_endpoint "
                "WHERE id = %s FOR UPDATE",
                [endpoint.id],
            )
            row = self.env.cr.fetchone()
            if not row:
                continue
            endpoint_values = {
                "revision": row[0] + 1,
                "subscription_state": "unknown",
                "observed_subscriptions_json": False,
                "verified_at": False,
                "last_error_class": False,
                "last_error_message": False,
            }
            super(MetaWebhookEndpoint, endpoint).write(endpoint_values)
            endpoint.flush_recordset(list(endpoint_values))
            endpoint.invalidate_recordset(["revision"])
        return True

    @api.model
    def _bounded_configuration_integer(
        self,
        parameter,
        default,
        minimum,
        maximum,
    ):
        raw_value = (
            self.env["ir.config_parameter"].sudo().get_param(parameter, str(default))
        )
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            return default
        return min(max(value, minimum), maximum)

    @api.model
    def _subscription_refresh_seconds(self):
        """Return when a fresh observation becomes eligible for refresh."""

        return self._bounded_configuration_integer(
            "meta_webhook_base.subscription_refresh_seconds",
            int(META_WEBHOOK_REFRESH_AFTER.total_seconds()),
            _SUBSCRIPTION_REFRESH_SECONDS_MIN,
            _SUBSCRIPTION_REFRESH_SECONDS_MAX,
        )

    @api.model
    def _subscription_reconcile_batch_limit(self):
        return self._bounded_configuration_integer(
            "meta_webhook_base.reconcile_batch_limit",
            _RECONCILE_BATCH_LIMIT,
            1,
            _RECONCILE_BATCH_LIMIT_MAX,
        )

    @api.model
    def _cron_reconcile_subscriptions(
        self,
        limit=None,
        refresh_seconds=None,
        now=None,
    ):
        """Enqueue one bounded recovery batch through the canonical service.

        ``error`` and ``unknown`` observations are immediately eligible.  Any
        other observation is refreshed well before the public freshness window
        expires.  The service owns queue ``identity_key`` deduplication, so an
        overlapping cron cannot create a second active reconciliation job.
        """

        if limit is None:
            limit = self._subscription_reconcile_batch_limit()
        elif isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValidationError(_("The Meta reconciliation batch is invalid."))
        limit = min(limit, _RECONCILE_BATCH_LIMIT_MAX)

        if refresh_seconds is None:
            refresh_seconds = self._subscription_refresh_seconds()
        elif (
            isinstance(refresh_seconds, bool)
            or not isinstance(refresh_seconds, int)
            or refresh_seconds <= 0
        ):
            raise ValidationError(_("The Meta subscription refresh is invalid."))
        refresh_seconds = min(
            max(refresh_seconds, _SUBSCRIPTION_REFRESH_SECONDS_MIN),
            _SUBSCRIPTION_REFRESH_SECONDS_MAX,
        )
        reference = fields.Datetime.to_datetime(now) if now else fields.Datetime.now()
        cutoff = reference - timedelta(seconds=refresh_seconds)
        active_job_uuids = (
            self.env["queue.job"]
            .sudo()
            .search(
                [
                    (
                        "identity_key",
                        "=like",
                        "meta_webhook:subscription:%",
                    ),
                    ("state", "in", _ACTIVE_QUEUE_JOB_STATES),
                ]
            )
            .mapped("uuid")
        )
        domain = [
            ("active", "=", True),
            ("app_id.active", "=", True),
            "|",
            "|",
            ("subscription_state", "in", ("unknown", "error")),
            ("verified_at", "=", False),
            ("verified_at", "<=", fields.Datetime.to_string(cutoff)),
        ]
        if active_job_uuids:
            domain = [
                "|",
                ("queue_job_uuid", "=", False),
                ("queue_job_uuid", "not in", active_job_uuids),
            ] + domain
        candidates = self.sudo().search(
            domain,
            order="verified_at, id",
            limit=limit,
        )
        service = self.env["meta.webhook.subscription.service"].sudo()
        processed = 0
        for endpoint in candidates:
            try:
                with self.env.cr.savepoint():
                    service._enqueue_endpoint(endpoint)
            except Exception:  # cron isolation boundary
                _logger.exception(
                    "Unable to enqueue Meta subscription reconciliation for %s",
                    endpoint.public_ref,
                )
                continue
            processed += 1
        return processed

    def action_enqueue_subscription_reconcile(self):
        self.ensure_one()
        self.check_access_rights("write")
        self.check_access_rule("write")
        job = self.env["meta.webhook.subscription.service"]._enqueue_endpoint(self)
        # Public object actions can be invoked through XML-RPC.  Never leak an
        # Odoo record/job proxy through that boundary; the UUID is sufficient
        # for callers to follow the enqueued (or reused) job.
        return str(job.uuid)

    def _job_reconcile_subscriptions(
        self, expected_endpoint_revision, expected_app_revision, expected_union_hash
    ):
        self.ensure_one()
        return self.env["meta.webhook.subscription.service"]._job_reconcile_endpoint(
            self,
            expected_endpoint_revision=expected_endpoint_revision,
            expected_app_revision=expected_app_revision,
            expected_union_hash=expected_union_hash,
        )

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Meta webhook endpoints must be archived."))

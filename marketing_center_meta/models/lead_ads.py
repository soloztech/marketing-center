import datetime
import hashlib
import re
import uuid

from psycopg2 import errors as pg_errors

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.marketing_center_base.services.dto import (
    MarketingIdentifierDTO,
    MarketingTouchpointDTO,
    canonical_json,
    sha256_text,
)
from odoo.addons.marketing_center_base.services.scheduler import fair_scheduler_batch
from odoo.addons.marketing_center_base.services.serialization import (
    MarketingSerializationFailure,
    acquire_advisory_xact_lock,
)
from odoo.addons.meta_api_base.services.errors import (
    MetaApiError,
    MetaApiPausedError,
    MetaApiRateLimitError,
    MetaApiTransientError,
)
from odoo.addons.meta_webhook_base.services.tokens import META_WEBHOOK_INTERNAL_TOKEN
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.adapter import META_ADS_SERVICE, MetaMarketingReadAdapter
from ..services.lead_ads import META_LEAD_CONTRACT_VERSION
from ..services.tokens import (
    MARKETING_META_LEAD_INTERNAL_TOKEN,
    MARKETING_META_LEAD_ROUTE_RUNTIME_TOKEN,
)

META_LEAD_CONSUMER_KEY = "marketing.lead_ads"
_ACTIVE_JOB_STATES = ("pending", "enqueued", "started", "wait_dependencies")
_ID_RE = re.compile(r"^[0-9]{1,40}$")
_SUBMISSION_ATTEMPT_CEILING = 8
_RECONCILE_ATTEMPT_CEILING = 8
_RECONCILE_MAX_PAGES = 10
_RECONCILE_SWEEP_MAX_PAGES = 1000
_RECONCILE_CURSOR_MAX_BYTES = 3072
_RECONCILE_ROUTE_LIMIT = 20
_WEBHOOK_REPLAY_LIMIT = 500
_RECONCILE_OVERLAP = datetime.timedelta(minutes=15)


def _uuid(_recordset):
    return str(uuid.uuid4())


def _internal(recordset):
    return (
        recordset.env.context.get("marketing_meta_lead_internal_token")
        is MARKETING_META_LEAD_INTERNAL_TOKEN
    )


def _route_runtime(recordset):
    return (
        recordset.env.context.get("marketing_meta_lead_route_runtime_token")
        is MARKETING_META_LEAD_ROUTE_RUNTIME_TOKEN
    )


def _job_attempt(record, job_uuid, persisted=0):
    job = (
        record.env["queue.job"].sudo().search([("uuid", "=", job_uuid)], limit=1)
        if job_uuid
        else record.env["queue.job"]
    )
    return max(int(persisted or 0) + 1, (job.retry + 1) if job else 1)


# Source lifecycle policy belongs with the Lead Ads route aggregate.
# pylint: disable=consider-merging-classes-inherited
class MarketingCenterSourceLeadAds(models.Model):
    _inherit = "marketing.center.source"

    def write(self, values):
        if values.get("active") is False:
            active_routes = (
                self.env["marketing.center.meta.lead.route"]
                .sudo()
                .search_count([("source_id", "in", self.ids), ("active", "=", True)])
            )
            if active_routes:
                raise ValidationError(
                    _(
                        "Archive active Lead Ads routes before archiving their "
                        "Meta Ads source."
                    )
                )
        return super().write(values)


class MarketingCenterMetaLeadRoute(models.Model):
    _name = "marketing.center.meta.lead.route"
    _description = "Marketing Center Meta Lead Ads Route"
    _order = "company_id, name, id"
    _check_company_auto = True

    name = fields.Char(required=True, index=True)
    active = fields.Boolean(default=True, index=True)
    public_ref = fields.Char(
        required=True,
        default=_uuid,
        size=36,
        readonly=True,
        copy=False,
        index=True,
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
        ondelete="restrict",
    )
    webhook_page_id = fields.Many2one(
        "meta.webhook.page",
        string="Webhook Page",
        required=True,
        index=True,
        check_company=True,
        ondelete="restrict",
    )
    meta_app_id = fields.Many2one(
        related="webhook_page_id.app_id",
        store=True,
        readonly=True,
        index=True,
    )
    lead_profile_id = fields.Many2one(
        "marketing.center.meta.profile",
        string="Lead reader profile",
        required=True,
        index=True,
        check_company=True,
        ondelete="restrict",
    )
    source_id = fields.Many2one(
        "marketing.center.source",
        string="Meta Ads source",
        index=True,
        check_company=True,
        ondelete="restrict",
        help=(
            "Mandatory source and roster boundary for every operational Lead Ads "
            "route. A source-less route may only exist as an inactive migration "
            "tombstone and cannot ingest or reconcile leads."
        ),
    )
    external_form_id = fields.Char(
        string="Meta Instant Form ID",
        required=True,
        size=40,
        index=True,
        copy=False,
    )
    route_revision = fields.Integer(
        required=True, default=1, readonly=True, copy=False, index=True
    )
    reconcile_enabled = fields.Boolean(default=True, index=True)
    reconcile_lookback_hours = fields.Integer(required=True, default=168)
    reconcile_state = fields.Selection(
        [
            ("idle", "Idle"),
            ("queued", "Queued"),
            ("running", "Running"),
            ("partial", "Partial"),
            ("error", "Error"),
            ("paused", "Paused"),
        ],
        required=True,
        default="idle",
        readonly=True,
        index=True,
    )
    reconcile_since = fields.Datetime(readonly=True, copy=False)
    reconcile_after = fields.Char(readonly=True, copy=False, size=3072)
    reconcile_cursor_hashes_json = fields.Json(
        readonly=True,
        copy=False,
        default=list,
        help=(
            "Hashes of provider cursors already visited by the current bounded "
            "sweep. They prevent non-immediate pagination cycles from running "
            "forever without persisting the provider cursor itself twice."
        ),
    )
    reconcile_page_count = fields.Integer(required=True, default=0, readonly=True)
    reconcile_attempts = fields.Integer(required=True, default=0, readonly=True)
    reconcile_job_uuid = fields.Char(readonly=True, copy=False, size=36, index=True)
    last_reconciled_at = fields.Datetime(readonly=True, copy=False, index=True)
    last_reconcile_error_class = fields.Char(readonly=True, copy=False, size=128)
    last_reconcile_error_message = fields.Char(readonly=True, copy=False, size=512)
    submission_ids = fields.One2many(
        "marketing.center.meta.lead.submission", "route_id", readonly=True
    )

    _sql_constraints = [
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The Lead Ads route public reference must be unique.",
        ),
        (
            "page_form_unique",
            "unique(webhook_page_id, external_form_id)",
            "This Meta Page and Instant Form route already exists.",
        ),
        (
            "route_counters_valid",
            "check(route_revision > 0 and reconcile_page_count >= 0 "
            "and reconcile_attempts >= 0 and reconcile_lookback_hours >= 1 "
            "and reconcile_lookback_hours <= 2160)",
            "The Lead Ads route counters are invalid.",
        ),
        (
            "active_source_required",
            "check(NOT active OR source_id IS NOT NULL)",
            "An active Lead Ads route must have a Meta Ads source.",
        ),
    ]

    @api.model
    def _runtime_fields(self):
        return {
            "reconcile_state",
            "reconcile_since",
            "reconcile_after",
            "reconcile_cursor_hashes_json",
            "reconcile_page_count",
            "reconcile_attempts",
            "reconcile_job_uuid",
            "last_reconciled_at",
            "last_reconcile_error_class",
            "last_reconcile_error_message",
        }

    @api.model_create_multi
    def create(self, vals_list):
        source_ids = sorted(
            {
                int(values["source_id"])
                for values in vals_list
                if values.get("source_id")
            }
        )
        sources = (
            self.env["marketing.center.source"]
            .browse(source_ids)
            ._lock_identity_scope()
        )
        prepared = []
        for incoming in vals_list:
            values = dict(incoming)
            if {"public_ref", "route_revision"}.intersection(values):
                raise AccessError(_("Lead Ads route identity is managed internally."))
            if self._runtime_fields().intersection(values) and not _route_runtime(self):
                raise AccessError(_("Lead Ads reconciliation is managed internally."))
            values["external_form_id"] = str(
                values.get("external_form_id") or ""
            ).strip()
            if not values.get("source_id"):
                raise ValidationError(
                    _("A new Lead Ads route requires an explicit Meta Ads source.")
                )
            values["reconcile_state"] = (
                "idle"
                if values.get("active", True) and values.get("reconcile_enabled", True)
                else "paused"
            )
            prepared.append(values)
        sources._mark_identity_evidence()
        return super().create(prepared)

    def write(self, values):
        values = dict(values)
        target_sources = self.env["marketing.center.source"]
        if values.get("source_id"):
            target_sources = target_sources.browse(
                [int(values["source_id"])]
            )._lock_identity_scope()
        if {
            "public_ref",
            "route_revision",
            "webhook_page_id",
            "company_id",
        }.intersection(values):
            raise AccessError(_("Lead Ads route identity is immutable."))
        if "external_form_id" in values and any(
            route.external_form_id != str(values["external_form_id"] or "").strip()
            for route in self
        ):
            raise AccessError(_("The Meta Instant Form ID is immutable."))
        if "source_id" in values and any(
            route.source_id and route.source_id.id != int(values.get("source_id") or 0)
            for route in self
        ):
            raise AccessError(
                _(
                    "The Lead Ads source is immutable after assignment; archive "
                    "the route and create a new one instead."
                )
            )
        if self._runtime_fields().intersection(values) and not _route_runtime(self):
            raise AccessError(_("Lead Ads reconciliation is managed internally."))
        lifecycle = {"active", "lead_profile_id", "source_id", "reconcile_enabled"}
        pages = (
            self.mapped("webhook_page_id") if lifecycle.intersection(values) else False
        )
        if "source_id" in values and target_sources:
            target_sources._mark_identity_evidence()
        if not lifecycle.intersection(values) or _route_runtime(self):
            return super().write(values)
        for route in self.sorted("id"):
            self.env.cr.execute(
                "SELECT route_revision FROM marketing_center_meta_lead_route "
                "WHERE id = %s FOR UPDATE",
                [route.id],
            )
            row = self.env.cr.fetchone()
            if not row:
                continue
            route_values = dict(values, route_revision=row[0] + 1)
            effective_active = route_values.get("active", route.active)
            effective_reconcile = route_values.get(
                "reconcile_enabled", route.reconcile_enabled
            )
            route_values.update(
                {
                    "reconcile_state": (
                        "idle" if effective_active and effective_reconcile else "paused"
                    ),
                    "reconcile_since": False,
                    "reconcile_after": False,
                    "reconcile_cursor_hashes_json": [],
                    "reconcile_page_count": 0,
                    "reconcile_attempts": 0,
                    "reconcile_job_uuid": False,
                    "last_reconcile_error_class": False,
                    "last_reconcile_error_message": False,
                }
            )
            super(MarketingCenterMetaLeadRoute, route).write(route_values)
        self._reconcile_subscription_lifecycle(pages)
        return True

    @api.constrains(
        "active",
        "company_id",
        "webhook_page_id",
        "lead_profile_id",
        "source_id",
        "external_form_id",
        "reconcile_lookback_hours",
    )
    def _check_route(self):
        for route in self:
            if not _ID_RE.fullmatch(route.external_form_id or ""):
                raise ValidationError(_("The Meta Instant Form ID is invalid."))
            if (
                route.webhook_page_id.company_id != route.company_id
                or route.lead_profile_id.company_id != route.company_id
                or route.lead_profile_id.meta_app_id != route.meta_app_id
                or route.lead_profile_id.reader_kind != "lead_reader"
            ):
                raise ValidationError(
                    _("The Lead Ads App, Page and reader profile are inconsistent.")
                )
            if route.active and not route.source_id:
                raise ValidationError(
                    _("An active Lead Ads route requires a Meta Ads source.")
                )
            if route.source_id and (
                route.source_id.company_id != route.company_id
                or route.source_id.service != META_ADS_SERVICE
                or (route.active and not route.source_id.active)
            ):
                raise ValidationError(
                    _(
                        "An active Lead Ads route requires an active Meta Ads "
                        "source in the same company."
                    )
                )

    def action_configure_webhook(self):
        self.ensure_one()
        self._check_admin()
        self.check_access_rights("write")
        self.check_access_rule("write")
        if (
            not self.active
            or not self.source_id.active
            or not self.webhook_page_id.active
        ):
            raise ValidationError(
                _("Activate the Lead Ads route, source and Page first.")
            )
        subscription_model = self.env["meta.webhook.subscription"].sudo()
        subscription = subscription_model.with_context(active_test=False).search(
            [
                ("page_id", "=", self.webhook_page_id.id),
                ("consumer_key", "=", META_LEAD_CONSUMER_KEY),
                ("object_type", "=", "page"),
                ("field_name", "=", "leadgen"),
            ],
            limit=1,
        )
        if subscription:
            if not subscription.active:
                subscription.write({"active": True})
        else:
            subscription_model.create(
                {
                    "page_id": self.webhook_page_id.id,
                    "consumer_key": META_LEAD_CONSUMER_KEY,
                    "object_type": "page",
                    "field_name": "leadgen",
                }
            )
        self.env["meta.webhook.subscription.service"]._enqueue_endpoint(
            self.webhook_page_id.endpoint_id
        )
        return True

    def action_enqueue_reconciliation(self):
        self.ensure_one()
        self._check_admin()
        self.check_access_rights("write")
        self.check_access_rule("write")
        self._enqueue_reconciliation()
        return True

    def _enqueue_reconciliation(self):
        self.ensure_one()
        route = self.sudo()
        route._lock_reconciliation_schedule()
        if (
            not route.active
            or not route.source_id.active
            or not route.reconcile_enabled
        ):
            return False
        current_job = route._current_reconcile_job()
        if current_job:
            return current_job
        continuing = route._continue_reconciliation_sweep()
        since = route.reconcile_since if continuing else route._new_reconcile_since()
        after = route.reconcile_after if continuing else ""
        page_number = route.reconcile_page_count if continuing else 0
        cursor_hashes = route._reconcile_cursor_hashes(continuing=continuing)
        identity_key = route._reconcile_identity_key(
            expected_route_revision=route.route_revision,
            expected_profile_revision=route.lead_profile_id.profile_revision,
            expected_app_revision=route.meta_app_id.revision,
            since=since,
            after=after,
        )
        active_job = route._active_reconcile_job(identity_key)
        if active_job:
            route._runtime_write(
                {
                    "reconcile_state": "queued",
                    "reconcile_cursor_hashes_json": cursor_hashes,
                    "reconcile_job_uuid": active_job.uuid,
                }
            )
            return active_job
        delayed = route.with_delay(
            identity_key=identity_key,
            max_retries=0,
            priority=35,
            description="Reconcile Meta leads %s" % route.public_ref,
        )._job_reconcile_meta_leads_page(
            expected_route_revision=route.route_revision,
            expected_profile_revision=route.lead_profile_id.profile_revision,
            expected_app_revision=route.meta_app_id.revision,
            since=fields.Datetime.to_string(since),
            after=after or "",
            page_number=page_number,
        )
        route._runtime_write(
            {
                "reconcile_state": "queued",
                "reconcile_since": since,
                "reconcile_after": after or False,
                "reconcile_cursor_hashes_json": cursor_hashes,
                "reconcile_page_count": page_number,
                "reconcile_attempts": 0,
                "reconcile_job_uuid": delayed.uuid,
                "last_reconcile_error_class": False,
                "last_reconcile_error_message": False,
            }
        )
        return delayed

    def _lock_reconciliation_schedule(self):
        """Serialize UI/cron scheduling with the worker's existing route lock."""
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM marketing_center_meta_lead_route WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset()

    def _current_reconcile_job(self):
        self.ensure_one()
        return (
            self.env["queue.job"]
            .sudo()
            .search(
                [
                    ("uuid", "=", self.reconcile_job_uuid or ""),
                    ("state", "in", _ACTIVE_JOB_STATES),
                ],
                limit=1,
            )
        )

    def _continue_reconciliation_sweep(self):
        self.ensure_one()
        return bool(self.reconcile_state == "partial" and self.reconcile_since)

    def _job_reconcile_meta_leads_page(
        self,
        *,
        expected_route_revision,
        expected_profile_revision,
        expected_app_revision,
        since,
        after,
        page_number,
    ):
        self.ensure_one()
        company = self.sudo().company_id
        route = (
            self.sudo()
            .with_company(company)
            .with_context(allowed_company_ids=[company.id])
        )
        return route.env["marketing.center.meta.lead.service"]._reconcile_page(
            route,
            expected_route_revision=expected_route_revision,
            expected_profile_revision=expected_profile_revision,
            expected_app_revision=expected_app_revision,
            since=since,
            after=after,
            page_number=page_number,
        )

    @api.model
    def _cron_enqueue_reconciliation(self, limit=_RECONCILE_ROUTE_LIMIT):
        limit = max(0, min(int(limit or 0), 100))
        if not limit:
            return 0
        routes = fair_scheduler_batch(
            self.env,
            self._name,
            [
                ("active", "=", True),
                ("source_id.active", "=", True),
                ("source_id.service", "=", META_ADS_SERVICE),
                ("reconcile_enabled", "=", True),
                ("reconcile_state", "in", ["idle", "partial", "error"]),
            ],
            cursor_key="meta.lead_reconciliation",
            limit=limit,
        )
        queued = 0
        for route in routes:
            scoped_route = route.with_company(route.company_id).with_context(
                allowed_company_ids=[route.company_id.id]
            )
            if scoped_route._enqueue_reconciliation():
                queued += 1
        return queued

    def _new_reconcile_since(self):
        self.ensure_one()
        now = fields.Datetime.now()
        if self.last_reconciled_at:
            return self.last_reconciled_at - _RECONCILE_OVERLAP
        return now - datetime.timedelta(hours=self.reconcile_lookback_hours)

    def _reconcile_identity_key(
        self,
        *,
        expected_route_revision,
        expected_profile_revision,
        expected_app_revision,
        since,
        after,
    ):
        self.ensure_one()
        since_text = fields.Datetime.to_string(since)
        cursor_digest = hashlib.sha256(
            ("%s\x00%s" % (since_text, after or "")).encode("utf-8")
        ).hexdigest()[:16]
        return "marketing_meta:lead_pull:%s:%s:%s:%s:%s" % (
            self.public_ref,
            expected_route_revision,
            expected_profile_revision,
            expected_app_revision,
            cursor_digest,
        )

    def _reconcile_cursor_hashes(self, *, continuing):
        """Return the validated cursor guard for one reconciliation sweep."""

        self.ensure_one()
        current_digest = self._reconcile_cursor_digest(self.reconcile_after or "")
        if not continuing:
            return [current_digest]
        hashes = self.reconcile_cursor_hashes_json
        if (
            not isinstance(hashes, list)
            or not hashes
            or len(hashes) > _RECONCILE_SWEEP_MAX_PAGES
            or any(
                not isinstance(value, str)
                or len(value) != 64
                or not re.fullmatch(r"[0-9a-f]{64}", value)
                for value in hashes
            )
            or len(set(hashes)) != len(hashes)
            or current_digest not in hashes
        ):
            raise ValidationError(_("The Lead Ads cursor history is invalid."))
        return list(hashes)

    @api.model
    def _reconcile_cursor_digest(self, cursor):
        if not isinstance(cursor, str):
            raise ValidationError(_("The Lead Ads reconciliation cursor is invalid."))
        try:
            encoded = cursor.encode("utf-8")
        except UnicodeEncodeError:
            raise ValidationError(
                _("The Lead Ads reconciliation cursor is invalid.")
            ) from None
        if len(encoded) > _RECONCILE_CURSOR_MAX_BYTES:
            raise ValidationError(_("The Lead Ads reconciliation cursor is invalid."))
        return hashlib.sha256(encoded).hexdigest()

    def _active_reconcile_job(self, identity_key):
        self.ensure_one()
        return (
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

    def _runtime_write(self, values):
        return self.with_context(
            marketing_meta_lead_route_runtime_token=(
                MARKETING_META_LEAD_ROUTE_RUNTIME_TOKEN
            )
        ).write(values)

    @api.model
    def _reconcile_subscription_lifecycle(self, pages):
        if not pages:
            return True
        subscriptions = self.env["meta.webhook.subscription"].sudo()
        service = self.env["meta.webhook.subscription.service"]
        for page in pages.sudo():
            active_route = self.sudo().search_count(
                [
                    ("webhook_page_id", "=", page.id),
                    ("active", "=", True),
                    ("source_id.active", "=", True),
                    ("source_id.service", "=", META_ADS_SERVICE),
                ]
            )
            subscription = subscriptions.search(
                [
                    ("page_id", "=", page.id),
                    ("consumer_key", "=", META_LEAD_CONSUMER_KEY),
                    ("object_type", "=", "page"),
                    ("field_name", "=", "leadgen"),
                    ("active", "=", True),
                ]
            )
            if not active_route and subscription:
                subscription.write({"active": False})
                if page.endpoint_id.active and page.app_id.active:
                    service._enqueue_endpoint(page.endpoint_id)
        return True

    def _check_admin(self):
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_admin"
        ):
            raise AccessError(
                _("Only Marketing Center administrators can manage Lead Ads routes.")
            )

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Lead Ads routes must be archived instead of deleted."))


class MarketingCenterMetaLeadSubmission(models.Model):
    _name = "marketing.center.meta.lead.submission"
    _description = "Marketing Center Meta Lead Submission"
    _order = "provider_created_at desc, id desc"
    _check_company_auto = True

    public_ref = fields.Char(
        required=True,
        default=_uuid,
        size=36,
        readonly=True,
        copy=False,
        index=True,
    )
    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict"
    )
    route_id = fields.Many2one(
        "marketing.center.meta.lead.route",
        required=True,
        index=True,
        check_company=True,
        ondelete="restrict",
    )
    meta_app_id = fields.Many2one(
        "meta.api.app",
        required=True,
        index=True,
        check_company=True,
        ondelete="restrict",
    )
    first_dispatch_id = fields.Many2one(
        "meta.webhook.dispatch",
        readonly=True,
        copy=False,
        ondelete="restrict",
        groups="base.group_system",
    )
    origin = fields.Selection(
        [("webhook", "Webhook"), ("reconciliation", "Reconciliation")],
        required=True,
        readonly=True,
        index=True,
    )
    leadgen_id = fields.Char(required=True, size=40, readonly=True, index=True)
    page_id_hint = fields.Char(required=True, size=40, readonly=True, index=True)
    form_id_hint = fields.Char(required=True, size=40, readonly=True, index=True)
    ad_id_hint = fields.Char(size=40, readonly=True, index=True)
    legacy_adgroup_id_hint = fields.Char(
        size=40,
        readonly=True,
        groups="marketing_center_base.group_marketing_center_admin",
        help=(
            "Legacy webhook diagnostic only. It is never promoted to a canonical "
            "Meta ad-set identifier."
        ),
    )
    hint_created_at = fields.Datetime(readonly=True, index=True)
    hint_sha256 = fields.Char(required=True, size=64, readonly=True)
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("ingested", "Ingested"),
            ("review", "Review"),
            ("blocked", "Blocked"),
            ("dead", "Dead"),
            ("stale", "Stale"),
        ],
        required=True,
        default="pending",
        readonly=True,
        index=True,
    )
    attempts = fields.Integer(required=True, default=0, readonly=True)
    queue_job_uuid = fields.Char(readonly=True, copy=False, size=36, index=True)
    processed_at = fields.Datetime(readonly=True, copy=False, index=True)
    provider_created_at = fields.Datetime(readonly=True, copy=False, index=True)
    provider_ad_id = fields.Char(readonly=True, copy=False, size=40, index=True)
    provider_campaign_id = fields.Char(readonly=True, copy=False, size=40, index=True)
    provider_payload_sha256 = fields.Char(readonly=True, copy=False, size=64)
    field_count = fields.Integer(required=True, default=0, readonly=True)
    touchpoint_id = fields.Many2one(
        "marketing.attribution.touchpoint",
        readonly=True,
        copy=False,
        check_company=True,
        ondelete="restrict",
    )
    last_error_class = fields.Char(readonly=True, copy=False, size=128)
    last_error_message = fields.Char(readonly=True, copy=False, size=512)
    field_ids = fields.One2many(
        "marketing.center.meta.lead.field",
        "submission_id",
        readonly=True,
        groups="base.group_system",
    )

    _sql_constraints = [
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The Meta lead submission public reference must be unique.",
        ),
        (
            "app_lead_unique",
            "unique(company_id, meta_app_id, leadgen_id)",
            "This Meta lead submission already exists.",
        ),
        (
            "submission_counters_valid",
            "check(attempts >= 0 and field_count >= 0)",
            "The Meta lead submission counters are invalid.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if not _internal(self):
            raise AccessError(_("Meta lead submissions are created internally."))
        return super().create(vals_list)

    def write(self, values):
        if not _internal(self):
            raise AccessError(_("Meta lead submissions are managed internally."))
        mutable = {
            "state",
            "attempts",
            "queue_job_uuid",
            "processed_at",
            "provider_created_at",
            "provider_ad_id",
            "provider_campaign_id",
            "provider_payload_sha256",
            "field_count",
            "touchpoint_id",
            "last_error_class",
            "last_error_message",
        }
        if set(values) - mutable:
            raise AccessError(_("Meta lead submission identity is immutable."))
        return super().write(values)

    def _enqueue_fetch(self):
        for submission in self.sudo().filtered(lambda item: item.state == "pending"):
            active = submission._active_job()
            if active:
                submission._internal_write({"queue_job_uuid": active.uuid})
                continue
            route = submission.route_id
            delayed = submission.with_delay(
                identity_key=submission._identity_key(),
                max_retries=0,
                priority=30,
                description="Retrieve Meta lead %s" % submission.public_ref,
            )._job_fetch_meta_lead(
                expected_route_revision=route.route_revision,
                expected_profile_revision=route.lead_profile_id.profile_revision,
                expected_app_revision=route.meta_app_id.revision,
            )
            submission._internal_write({"queue_job_uuid": delayed.uuid})
        return True

    def _job_fetch_meta_lead(
        self,
        *,
        expected_route_revision,
        expected_profile_revision,
        expected_app_revision,
    ):
        self.ensure_one()
        company = self.sudo().company_id
        submission = (
            self.sudo()
            .with_company(company)
            .with_context(allowed_company_ids=[company.id])
        )
        return submission.env[
            "marketing.center.meta.lead.service"
        ]._retrieve_submission(
            submission,
            expected_route_revision=expected_route_revision,
            expected_profile_revision=expected_profile_revision,
            expected_app_revision=expected_app_revision,
        )

    def _identity_key(self):
        self.ensure_one()
        return "marketing_meta:lead:%s:%s:%s" % (
            self.company_id.id,
            self.meta_app_id.public_ref,
            self.leadgen_id,
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

    def _internal_write(self, values):
        return self.with_context(
            marketing_meta_lead_internal_token=MARKETING_META_LEAD_INTERNAL_TOKEN
        ).write(values)

    def action_requeue(self):
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_admin"
        ):
            raise AccessError(
                _("Only Marketing Center administrators can requeue Meta leads.")
            )
        self.check_access_rights("read")
        self.check_access_rule("read")
        for submission in self:
            if submission.state not in {"review", "blocked", "dead", "stale"}:
                raise ValidationError(_("This Meta lead cannot be requeued."))
            if submission._active_job():
                raise ValidationError(_("Wait for the active Meta lead job."))
        internal = self.sudo().with_context(
            marketing_meta_lead_internal_token=MARKETING_META_LEAD_INTERNAL_TOKEN
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
        return internal._enqueue_fetch()

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Meta lead submissions are immutable."))


class MarketingCenterMetaLeadField(models.Model):
    _name = "marketing.center.meta.lead.field"
    _description = "Private Meta Lead Field"
    _order = "submission_id, sequence, id"
    _check_company_auto = True

    submission_id = fields.Many2one(
        "marketing.center.meta.lead.submission",
        required=True,
        index=True,
        ondelete="restrict",
    )
    company_id = fields.Many2one(
        related="submission_id.company_id", store=True, readonly=True, index=True
    )
    sequence = fields.Integer(required=True, readonly=True)
    field_name = fields.Char(required=True, size=128, readonly=True, index=True)
    values_json = fields.Json(required=True, readonly=True, groups="base.group_system")
    value_count = fields.Integer(required=True, readonly=True)
    values_sha256 = fields.Char(required=True, size=64, readonly=True)

    _sql_constraints = [
        (
            "submission_field_unique",
            "unique(submission_id, field_name)",
            "This Meta lead field was already stored.",
        ),
        (
            "field_counters_valid",
            "check(sequence >= 0 and value_count >= 0)",
            "The Meta lead field counters are invalid.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if not _internal(self):
            raise AccessError(_("Meta lead fields are created internally."))
        return super().create(vals_list)

    def write(self, values):  # pylint: disable=method-required-super
        raise AccessError(_("Meta lead fields are immutable."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Meta lead fields are immutable."))


class MarketingCenterMetaLeadService(models.AbstractModel):
    _name = "marketing.center.meta.lead.service"
    _description = "Marketing Center Meta Lead Ads Service"

    @api.model
    def _retrieve_submission(
        self,
        submission,
        *,
        expected_route_revision,
        expected_profile_revision,
        expected_app_revision,
    ):
        submission = submission.sudo().exists()
        if not submission or len(submission) != 1:
            return False
        attempt = self._begin_submission(
            submission,
            expected_route_revision,
            expected_profile_revision,
            expected_app_revision,
        )
        if not attempt:
            return False
        route = submission.route_id
        try:
            lead = MetaMarketingReadAdapter(
                route.lead_profile_id,
                expected_app_revision=expected_app_revision,
            ).fetch_lead(submission.leadgen_id)
        except MetaApiRateLimitError as error:
            return self._retry_or_finish(
                submission,
                attempt,
                error,
                retry_message="Meta lead retrieval is rate limited",
                retry_seconds=error.retry_after_seconds or None,
            )
        except MetaApiTransientError as error:
            return self._retry_or_finish(
                submission,
                attempt,
                error,
                retry_message="Meta lead retrieval is temporarily unavailable",
            )
        except MetaApiPausedError:
            self._finish_submission(
                submission,
                "blocked",
                "AuthorizationUnavailable",
                "Meta Lead Ads authorization is unavailable.",
            )
            return False
        except MetaApiError:
            self._finish_submission(
                submission,
                "review",
                "ProviderPayloadInvalid",
                "Meta lead retrieval could not be normalized.",
            )
            return False
        return self._apply_lead(
            submission,
            lead,
            expected_route_revision=expected_route_revision,
            expected_profile_revision=expected_profile_revision,
            expected_app_revision=expected_app_revision,
        )

    @api.model
    def _reconcile_page(
        self,
        route,
        *,
        expected_route_revision,
        expected_profile_revision,
        expected_app_revision,
        since,
        after,
        page_number,
    ):
        route = route.sudo().exists()
        if not route or len(route) != 1:
            return False
        job_uuid = self.env.context.get("job_uuid")
        route.flush_recordset(["reconcile_job_uuid"])
        route.env.cr.execute(
            "SELECT reconcile_job_uuid "
            "FROM marketing_center_meta_lead_route "
            "WHERE id = %s FOR UPDATE",
            [route.id],
        )
        ownership = route.env.cr.fetchone()
        route.invalidate_recordset(["reconcile_job_uuid"])
        if (
            not ownership
            or not isinstance(job_uuid, str)
            or not job_uuid
            or ownership[0] != job_uuid
        ):
            return False
        if (
            not self._route_is_current(
                route,
                expected_route_revision,
                expected_profile_revision,
                expected_app_revision,
            )
            or not route.reconcile_enabled
        ):
            return self._pause_reconcile_revision(route)
        parsed_since = fields.Datetime.to_datetime(since)
        if not parsed_since or page_number < 0 or page_number >= _RECONCILE_MAX_PAGES:
            raise ValidationError(_("The Lead Ads reconciliation cursor is invalid."))
        attempt = _job_attempt(
            route, self.env.context.get("job_uuid"), route.reconcile_attempts
        )
        route._runtime_write(
            {"reconcile_state": "running", "reconcile_attempts": attempt}
        )
        page = self._fetch_reconcile_page(
            route,
            expected_app_revision=expected_app_revision,
            parsed_since=parsed_since,
            after=after,
            attempt=attempt,
        )
        if not page:
            return False
        if not self._route_is_current(
            route,
            expected_route_revision,
            expected_profile_revision,
            expected_app_revision,
        ):
            return self._pause_reconcile_revision(route)
        self._project_reconcile_leads(
            route,
            page.leads,
            expected_route_revision=expected_route_revision,
            expected_profile_revision=expected_profile_revision,
            expected_app_revision=expected_app_revision,
        )
        return self._advance_reconcile_page(
            route,
            page,
            expected_route_revision=expected_route_revision,
            expected_profile_revision=expected_profile_revision,
            expected_app_revision=expected_app_revision,
            parsed_since=parsed_since,
            page_number=page_number,
        )

    @api.model
    def _fetch_reconcile_page(
        self, route, *, expected_app_revision, parsed_since, after, attempt
    ):
        try:
            return MetaMarketingReadAdapter(
                route.lead_profile_id,
                expected_app_revision=expected_app_revision,
            ).fetch_lead_page(
                route.external_form_id,
                after=after,
                since=parsed_since,
            )
        except MetaApiRateLimitError as error:
            if attempt < _RECONCILE_ATTEMPT_CEILING:
                raise RetryableJobError(
                    "Meta lead reconciliation is rate limited",
                    seconds=error.retry_after_seconds or None,
                ) from None
            self._finish_reconcile_error(route, "RateLimit")
        except MetaApiTransientError:
            if attempt < _RECONCILE_ATTEMPT_CEILING:
                raise RetryableJobError(
                    "Meta lead reconciliation is temporarily unavailable"
                ) from None
            self._finish_reconcile_error(route, "TransientRetryLimit")
        except MetaApiPausedError:
            self._finish_reconcile_error(route, "AuthorizationUnavailable")
        except MetaApiError:
            self._finish_reconcile_error(route, "ProviderPayloadInvalid")
        return False

    @api.model
    def _project_reconcile_leads(
        self,
        route,
        leads,
        *,
        expected_route_revision,
        expected_profile_revision,
        expected_app_revision,
    ):
        for lead in leads:
            submission = self._find_or_create_submission_from_lead(route, lead)
            try:
                with self.env.cr.savepoint():
                    if not self._claim_reconcile_submission(submission, lead):
                        continue
                    self._apply_lead(
                        submission,
                        lead,
                        expected_route_revision=expected_route_revision,
                        expected_profile_revision=expected_profile_revision,
                        expected_app_revision=expected_app_revision,
                    )
            except (ValidationError, AccessError):
                self._finish_reconcile_projection_error(submission)
        return True

    @api.model
    def _claim_reconcile_submission(self, submission, lead):
        """Claim one projection without waiting for its webhook retrieval job."""

        submission.flush_recordset(["state", "attempts", "provider_payload_sha256"])
        self.env.cr.execute(
            "SELECT state, attempts, provider_payload_sha256 "
            "FROM marketing_center_meta_lead_submission "
            "WHERE id = %s FOR UPDATE SKIP LOCKED",
            [submission.id],
        )
        row = self.env.cr.fetchone()
        if not row:
            # A webhook worker (or another pull worker) owns the submission.
            # Its authenticated projection is authoritative for this pass.
            return False
        state, attempts, payload_sha256 = row
        submission.invalidate_recordset(
            ["state", "attempts", "provider_payload_sha256"]
        )
        if state == "ingested":
            if payload_sha256 != lead.payload_sha256:
                submission._internal_write(
                    {
                        "state": "review",
                        "processed_at": fields.Datetime.now(),
                        "last_error_class": "ImmutablePayloadConflict",
                        "last_error_message": (
                            "The authenticated Meta lead changed after ingestion."
                        ),
                    }
                )
            return False
        if state == "review":
            # Reconciliation must never release an operator quarantine.
            return False
        if state not in {"pending", "processing", "blocked", "dead", "stale"}:
            return False
        submission._internal_write(
            {"state": "processing", "attempts": max(1, int(attempts or 0))}
        )
        return True

    @api.model
    def _finish_reconcile_projection_error(self, submission):
        """Quarantine a rejected pull unless a concurrent job already won."""

        submission.flush_recordset(["state"])
        self.env.cr.execute(
            "SELECT state FROM marketing_center_meta_lead_submission "
            "WHERE id = %s FOR UPDATE",
            [submission.id],
        )
        row = self.env.cr.fetchone()
        submission.invalidate_recordset(["state"])
        if not row or row[0] == "ingested":
            return False
        submission._internal_write(
            {
                "state": "review",
                "processed_at": fields.Datetime.now(),
                "last_error_class": "ProjectionRejected",
                "last_error_message": (
                    "The authenticated Meta lead could not be projected."
                ),
            }
        )
        return True

    @api.model
    def _advance_reconcile_page(
        self,
        route,
        page,
        *,
        expected_route_revision,
        expected_profile_revision,
        expected_app_revision,
        parsed_since,
        page_number,
    ):
        completed_pages = page_number + 1
        cursor_hashes = []
        if page.has_more:
            cursor_hashes = self._next_reconcile_cursor_hashes(route, page.next_after)
            if not cursor_hashes:
                return False
        if page.has_more and completed_pages < _RECONCILE_MAX_PAGES:
            return self._chain_reconcile_page(
                route,
                expected_route_revision,
                expected_profile_revision,
                expected_app_revision,
                parsed_since,
                page.next_after,
                completed_pages,
                cursor_hashes,
            )
        if page.has_more:
            route._runtime_write(
                {
                    "reconcile_state": "partial",
                    "reconcile_after": page.next_after,
                    "reconcile_cursor_hashes_json": cursor_hashes,
                    # The cursor is durable across bounded chunks; the page
                    # counter is per chunk so the next cron can safely resume.
                    "reconcile_page_count": 0,
                    "reconcile_attempts": 0,
                    "reconcile_job_uuid": False,
                    "last_reconcile_error_class": False,
                    "last_reconcile_error_message": False,
                }
            )
            return {"partial": True, "pages": completed_pages}
        route._runtime_write(
            {
                "reconcile_state": "idle",
                "reconcile_since": False,
                "reconcile_after": False,
                "reconcile_cursor_hashes_json": [],
                "reconcile_page_count": 0,
                "reconcile_attempts": 0,
                "reconcile_job_uuid": False,
                "last_reconciled_at": fields.Datetime.now(),
                "last_reconcile_error_class": False,
                "last_reconcile_error_message": False,
            }
        )
        return {"done": True, "pages": completed_pages}

    @api.model
    def _next_reconcile_cursor_hashes(self, route, next_after):
        hashes = route._reconcile_cursor_hashes(continuing=True)
        digest = route._reconcile_cursor_digest(next_after)
        if digest in hashes:
            self._finish_reconcile_error(route, "ProviderCursorCycle")
            return []
        if len(hashes) >= _RECONCILE_SWEEP_MAX_PAGES:
            self._finish_reconcile_error(route, "ProviderPageLimit")
            return []
        return hashes + [digest]

    @api.model
    def _pause_reconcile_revision(self, route):
        route._runtime_write(
            {
                "reconcile_state": "paused",
                "reconcile_job_uuid": False,
                "last_reconcile_error_class": "RouteRevisionChanged",
                "last_reconcile_error_message": (
                    "Lead Ads route configuration changed during reconciliation."
                ),
            }
        )
        return False

    @api.model
    def _begin_submission(
        self,
        submission,
        expected_route_revision,
        expected_profile_revision,
        expected_app_revision,
    ):
        submission.flush_recordset(["state", "attempts", "queue_job_uuid"])
        self.env.cr.execute(
            "SELECT state, attempts, queue_job_uuid "
            "FROM marketing_center_meta_lead_submission WHERE id = %s FOR UPDATE",
            [submission.id],
        )
        row = self.env.cr.fetchone()
        if not row or row[0] not in {"pending", "processing"}:
            return 0
        job_uuid = self.env.context.get("job_uuid")
        if not isinstance(job_uuid, str) or not job_uuid or row[2] != job_uuid:
            return 0
        if not self._route_is_current(
            submission.route_id,
            expected_route_revision,
            expected_profile_revision,
            expected_app_revision,
        ):
            submission._internal_write(
                {
                    "state": "stale",
                    "processed_at": fields.Datetime.now(),
                    "last_error_class": "RouteRevisionChanged",
                    "last_error_message": (
                        "Lead Ads route configuration changed before retrieval."
                    ),
                }
            )
            return 0
        attempt = _job_attempt(submission, job_uuid, row[1])
        submission._internal_write({"state": "processing", "attempts": attempt})
        return attempt

    @api.model
    def _apply_lead(
        self,
        submission,
        lead,
        *,
        expected_route_revision,
        expected_profile_revision,
        expected_app_revision,
    ):
        route = submission.route_id
        if not self._route_is_current(
            route,
            expected_route_revision,
            expected_profile_revision,
            expected_app_revision,
        ):
            self._finish_submission(
                submission,
                "stale",
                "RouteRevisionChanged",
                "Lead Ads route configuration changed during retrieval.",
            )
            return False
        if (
            lead.leadgen_id != submission.leadgen_id
            or lead.form_id != route.external_form_id
            or submission.form_id_hint != route.external_form_id
            or submission.page_id_hint != route.webhook_page_id.external_page_id
        ):
            self._finish_submission(
                submission,
                "review",
                "RouteMismatch",
                "The authenticated Meta lead does not match its strict route.",
            )
            return False
        if submission.ad_id_hint and lead.ad_id and submission.ad_id_hint != lead.ad_id:
            self._finish_submission(
                submission,
                "review",
                "AdIdentityMismatch",
                "The webhook hint and authenticated Meta lead disagree on the ad.",
            )
            return False
        existing_fields = submission.sudo().field_ids
        if existing_fields:
            observed = {
                item.field_name: (tuple(item.values_json or ()), item.values_sha256)
                for item in existing_fields
            }
            expected = {
                item.name: (
                    item.values,
                    sha256_text(canonical_json(list(item.values))),
                )
                for item in lead.fields
            }
            if observed != expected:
                self._finish_submission(
                    submission,
                    "review",
                    "ImmutablePayloadConflict",
                    "The authenticated Meta lead changed after it was first observed.",
                )
                return False
        else:
            self.env["marketing.center.meta.lead.field"].sudo().with_context(
                marketing_meta_lead_internal_token=MARKETING_META_LEAD_INTERNAL_TOKEN
            ).create(
                [
                    {
                        "submission_id": submission.id,
                        "sequence": sequence,
                        "field_name": item.name,
                        "values_json": list(item.values),
                        "value_count": len(item.values),
                        "values_sha256": sha256_text(canonical_json(list(item.values))),
                    }
                    for sequence, item in enumerate(lead.fields)
                ]
            )
        result = self.env["marketing.attribution.service"]._ingest_touchpoint(
            submission.company_id,
            self._lead_touchpoint(submission, lead),
        )
        submission._internal_write(
            {
                "state": "ingested",
                "processed_at": fields.Datetime.now(),
                "provider_created_at": lead.created_at,
                "provider_ad_id": lead.ad_id or False,
                "provider_campaign_id": lead.campaign_id or False,
                "provider_payload_sha256": lead.payload_sha256,
                "field_count": len(lead.fields),
                "touchpoint_id": result.touchpoint_id,
                "last_error_class": False,
                "last_error_message": False,
            }
        )
        return result

    @api.model
    def _lead_touchpoint(self, submission, lead):
        route = submission.route_id
        asset_refs = {
            "meta.page_id": route.webhook_page_id.external_page_id,
            "meta.form_id": route.external_form_id,
            "meta.ad_account_ref": route.source_id.external_account_ref,
        }
        if lead.ad_id:
            asset_refs["meta.ad_id"] = lead.ad_id
        if lead.campaign_id:
            asset_refs["meta.campaign_id"] = lead.campaign_id
        return MarketingTouchpointDTO(
            source_system="meta.lead_ads",
            source_scope_ref="meta.app:%s:page:%s:form:%s"
            % (
                route.meta_app_id.external_app_id,
                route.webhook_page_id.external_page_id,
                route.external_form_id,
            ),
            source_occurrence_ref="leadgen:%s" % lead.leadgen_id,
            source_evidence_ref="meta.lead.submission:%s" % submission.public_ref,
            source_schema_version=META_LEAD_CONTRACT_VERSION,
            occurred_at=lead.created_at,
            observed_at=fields.Datetime.now(),
            platform="meta",
            channel="lead_ads",
            network="facebook",
            touchpoint_type="lead_ad",
            evidence_level="provider_asserted",
            asset_refs=asset_refs,
            identifiers=self._lead_identifiers(lead),
            extensions={
                "meta.lead_ads": {
                    "field_count": len(lead.fields),
                    "payload_sha256": lead.payload_sha256,
                }
            },
        )

    @api.model
    def _lead_identifiers(self, lead):
        identifiers = {}
        for field in lead.fields:
            if field.name == "email":
                for value in field.values:
                    normalized = value.strip().lower()
                    if "@" not in normalized or len(normalized) > 320:
                        continue
                    key = ("email", sha256_text(normalized))
                    identifiers[key] = MarketingIdentifierDTO(
                        namespace="email",
                        role="lead",
                        comparison_hash=key[1],
                        masked_value=self._mask_email(normalized),
                        source_field=field.name,
                    )
            elif field.name in {"phone", "phone_number"}:
                for value in field.values:
                    digits = "".join(
                        character for character in value if character.isdigit()
                    )
                    if not 7 <= len(digits) <= 15:
                        continue
                    key = ("phone", sha256_text(digits))
                    identifiers[key] = MarketingIdentifierDTO(
                        namespace="phone",
                        role="lead",
                        comparison_hash=key[1],
                        masked_value="***%s" % digits[-4:],
                        source_field=field.name,
                    )
        return tuple(identifiers[key] for key in sorted(identifiers))

    @api.model
    def _mask_email(self, value):
        local, domain = value.rsplit("@", 1)
        return "%s***@%s" % (local[:1], domain)

    @api.model
    def _find_or_create_submission_from_lead(self, route, lead):
        return self._find_or_create_submission(
            route,
            leadgen_id=lead.leadgen_id,
            origin="reconciliation",
            page_id=route.webhook_page_id.external_page_id,
            form_id=lead.form_id,
            ad_id=lead.ad_id,
            legacy_adgroup_id="",
            hint_created_at=lead.created_at,
            hint_sha256=lead.payload_sha256,
        )

    @api.model
    def _find_or_create_submission(
        self,
        route,
        *,
        leadgen_id,
        origin,
        page_id,
        form_id,
        ad_id,
        legacy_adgroup_id,
        hint_created_at,
        hint_sha256,
        dispatch=None,
    ):
        if (
            not route.active
            or not route.source_id.active
            or route.source_id.company_id != route.company_id
            or route.source_id.service != META_ADS_SERVICE
        ):
            raise ValidationError(
                _("Meta leads require an active route with a valid source scope.")
            )
        domain = [
            ("company_id", "=", route.company_id.id),
            ("meta_app_id", "=", route.meta_app_id.id),
            ("leadgen_id", "=", leadgen_id),
        ]
        model = self.env["marketing.center.meta.lead.submission"].sudo()
        lock_key = "marketing_meta_lead:%s:%s:%s" % (
            route.company_id.id,
            route.meta_app_id.id,
            leadgen_id,
        )
        acquire_advisory_xact_lock(
            self.env.cr,
            lock_key,
            "Concurrent Meta lead submission requires a fresh snapshot",
        )
        submission = model.search(domain, limit=1)
        if submission:
            if submission.route_id != route:
                submission._internal_write(
                    {
                        "state": "review",
                        "processed_at": fields.Datetime.now(),
                        "last_error_class": "AmbiguousLeadRoute",
                        "last_error_message": (
                            "The same Meta lead was observed on another strict route."
                        ),
                    }
                )
            elif (
                submission.page_id_hint != page_id
                or submission.form_id_hint != form_id
                or (submission.ad_id_hint and ad_id and submission.ad_id_hint != ad_id)
            ):
                submission._internal_write(
                    {
                        "state": "review",
                        "processed_at": fields.Datetime.now(),
                        "last_error_class": "WebhookHintConflict",
                        "last_error_message": (
                            "Conflicting Meta routing hints were observed for one lead."
                        ),
                    }
                )
            return submission
        try:
            with self.env.cr.savepoint():
                return model.with_context(
                    marketing_meta_lead_internal_token=(
                        MARKETING_META_LEAD_INTERNAL_TOKEN
                    )
                ).create(
                    {
                        "company_id": route.company_id.id,
                        "route_id": route.id,
                        "meta_app_id": route.meta_app_id.id,
                        "first_dispatch_id": dispatch.id if dispatch else False,
                        "origin": origin,
                        "leadgen_id": leadgen_id,
                        "page_id_hint": page_id,
                        "form_id_hint": form_id,
                        "ad_id_hint": ad_id or False,
                        "legacy_adgroup_id_hint": legacy_adgroup_id or False,
                        "hint_created_at": hint_created_at or False,
                        "hint_sha256": hint_sha256,
                    }
                )
        except pg_errors.UniqueViolation as error:
            if error.diag.constraint_name != (
                "marketing_center_meta_lead_submission_app_lead_unique"
            ):
                raise
            # Another writer may have committed after this transaction's
            # earlier route lookup established its REPEATABLE READ snapshot.
            # Searching again cannot observe that row; retry the transaction.
            raise MarketingSerializationFailure(
                "Concurrent Meta lead submission requires a fresh snapshot"
            ) from None

    @api.model
    def _route_is_current(
        self,
        route,
        expected_route_revision,
        expected_profile_revision,
        expected_app_revision,
    ):
        route.invalidate_recordset(
            [
                "active",
                "route_revision",
                "lead_profile_id",
                "meta_app_id",
                "source_id",
                "company_id",
            ]
        )
        route.source_id.invalidate_recordset(["active", "service", "company_id"])
        route.lead_profile_id.invalidate_recordset(
            ["active", "reader_kind", "profile_revision", "meta_app_id"]
        )
        route.meta_app_id.invalidate_recordset(["active", "revision"])
        return bool(
            route.active
            and route.route_revision == expected_route_revision
            and route.source_id.active
            and route.source_id.service == META_ADS_SERVICE
            and route.source_id.company_id == route.company_id
            and route.webhook_page_id.active
            and route.webhook_page_id.endpoint_id.active
            and route.webhook_page_id.app_id == route.meta_app_id
            and route.webhook_page_id.company_id == route.company_id
            and route.lead_profile_id.active
            and route.lead_profile_id.reader_kind == "lead_reader"
            and route.lead_profile_id.profile_revision == expected_profile_revision
            and route.lead_profile_id.meta_app_id == route.meta_app_id
            and route.meta_app_id.active
            and route.meta_app_id.revision == expected_app_revision
        )

    @api.model
    def _retry_or_finish(
        self,
        submission,
        attempt,
        error,
        *,
        retry_message,
        retry_seconds=None,
    ):
        if attempt < _SUBMISSION_ATTEMPT_CEILING:
            raise RetryableJobError(retry_message, seconds=retry_seconds) from None
        self._finish_submission(
            submission,
            "dead",
            "ProviderRetryLimit",
            "Meta lead retrieval retry limit was reached.",
        )
        return False

    @api.model
    def _finish_submission(self, submission, state, error_class, message):
        submission._internal_write(
            {
                "state": state,
                "processed_at": fields.Datetime.now(),
                "last_error_class": error_class,
                "last_error_message": message,
            }
        )

    @api.model
    def _finish_reconcile_error(self, route, error_class):
        route._runtime_write(
            {
                "reconcile_state": "error",
                "reconcile_job_uuid": False,
                "last_reconcile_error_class": error_class,
                "last_reconcile_error_message": (
                    "Meta Lead Ads reconciliation requires operator attention."
                ),
            }
        )
        return False

    @api.model
    def _chain_reconcile_page(
        self,
        route,
        expected_route_revision,
        expected_profile_revision,
        expected_app_revision,
        since,
        after,
        page_number,
        cursor_hashes,
    ):
        identity_key = route._reconcile_identity_key(
            expected_route_revision=expected_route_revision,
            expected_profile_revision=expected_profile_revision,
            expected_app_revision=expected_app_revision,
            since=since,
            after=after,
        )
        delayed = route.with_delay(
            identity_key=identity_key,
            max_retries=0,
            priority=35,
            description="Reconcile Meta leads %s page %s"
            % (route.public_ref, page_number + 1),
        )._job_reconcile_meta_leads_page(
            expected_route_revision=expected_route_revision,
            expected_profile_revision=expected_profile_revision,
            expected_app_revision=expected_app_revision,
            since=fields.Datetime.to_string(since),
            after=after,
            page_number=page_number,
        )
        route._runtime_write(
            {
                "reconcile_state": "queued",
                "reconcile_after": after,
                "reconcile_cursor_hashes_json": cursor_hashes,
                "reconcile_page_count": page_number,
                "reconcile_attempts": 0,
                "reconcile_job_uuid": delayed.uuid,
            }
        )
        return {"continued": True, "pages": page_number}


class MarketingCenterMetaLeadWebhookDispatcher(models.AbstractModel):
    _inherit = "meta.webhook.dispatcher"

    @api.model
    def _after_subscription_reconcile(self, endpoint):
        result = super()._after_subscription_reconcile(endpoint)
        routes = (
            self.env["marketing.center.meta.lead.route"]
            .sudo()
            .search(
                [
                    ("active", "=", True),
                    ("source_id.active", "=", True),
                    ("source_id.service", "=", META_ADS_SERVICE),
                    ("webhook_page_id.endpoint_id", "=", endpoint.id),
                ]
            )
        )
        route_keys = {
            (route.webhook_page_id.external_page_id, route.external_form_id)
            for route in routes
        }
        if not route_keys:
            return result
        dispatches = (
            self.env["meta.webhook.dispatch"]
            .sudo()
            .search(
                [
                    ("delivery_id.endpoint_id", "=", endpoint.id),
                    ("consumer_key", "=", META_LEAD_CONSUMER_KEY),
                    ("state", "=", "unrouted"),
                ],
                order="id",
                limit=_WEBHOOK_REPLAY_LIMIT,
            )
        )
        recover_dispatches = dispatches.filtered(
            lambda dispatch: self._lead_item_route_key(dispatch.item_id) in route_keys
        )
        if recover_dispatches:
            recover_dispatches.with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            ).write(
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
            recover_dispatches._enqueue()
        deliveries = (
            self.env["meta.webhook.delivery"]
            .sudo()
            .search(
                [
                    ("endpoint_id", "=", endpoint.id),
                    ("state", "=", "unrouted"),
                    ("item_ids.kind", "=", "leadgen"),
                ],
                order="id",
                limit=_WEBHOOK_REPLAY_LIMIT,
            )
        )
        recover_deliveries = deliveries.filtered(
            lambda delivery: any(
                self._lead_item_route_key(item) in route_keys
                for item in delivery.item_ids
            )
        )
        if recover_deliveries:
            recover_deliveries.with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            ).write(
                {
                    "state": "pending",
                    "attempts": 0,
                    "queue_job_uuid": False,
                    "processed_at": False,
                    "last_error_class": False,
                    "last_error_message": False,
                }
            )
            recover_deliveries._enqueue()
        return result

    @api.model
    def _lead_item_route_key(self, item):
        payload = item.payload_json if item.kind == "leadgen" else {}
        value = payload.get("value") if isinstance(payload, dict) else {}
        page_id = str(value.get("page_id") or "") if isinstance(value, dict) else ""
        form_id = str(value.get("form_id") or "") if isinstance(value, dict) else ""
        if not _ID_RE.fullmatch(page_id) or not _ID_RE.fullmatch(form_id):
            return ()
        return page_id, form_id

    @api.model
    def _dispatch_consumer(self, dispatch):
        result = super()._dispatch_consumer(dispatch)
        if result is not None or dispatch.consumer_key != META_LEAD_CONSUMER_KEY:
            return result
        submission = self._project_lead_hint(dispatch)
        if not submission:
            return None
        submission._enqueue_fetch()
        return {
            "handled": True,
            "result_ref": "marketing.center.meta.lead.submission:%s" % submission.id,
        }

    @api.model
    def _project_lead_hint(self, dispatch):
        dispatch.ensure_one()
        company = dispatch.sudo().company_id
        dispatch = (
            dispatch.sudo()
            .with_company(company)
            .with_context(allowed_company_ids=[company.id])
        )
        item = dispatch.item_id
        payload = item.payload_json
        value = payload.get("value") if isinstance(payload, dict) else None
        entry = payload.get("entry") if isinstance(payload, dict) else None
        if (
            item.kind != "leadgen"
            or item.object_type != "page"
            or item.event_field != "leadgen"
            or not isinstance(value, dict)
            or not isinstance(entry, dict)
            or payload.get("object") != "page"
            or payload.get("field") != "leadgen"
        ):
            return self.env["marketing.center.meta.lead.submission"]
        page_id = str(value.get("page_id") or "")
        form_id = str(value.get("form_id") or "")
        leadgen_id = str(value.get("leadgen_id") or "")
        if not all(_ID_RE.fullmatch(value) for value in (page_id, form_id, leadgen_id)):
            return self.env["marketing.center.meta.lead.submission"]
        if (
            page_id != item.target_asset_id
            or page_id != entry.get("id")
            or dispatch.page_id.external_page_id != page_id
            or dispatch.page_id.app_id != dispatch.delivery_id.app_id
            or dispatch.page_id.endpoint_id != dispatch.delivery_id.endpoint_id
        ):
            return self.env["marketing.center.meta.lead.submission"]
        routes = (
            self.env["marketing.center.meta.lead.route"]
            .sudo()
            .search(
                [
                    ("company_id", "=", dispatch.company_id.id),
                    ("active", "=", True),
                    ("source_id.active", "=", True),
                    ("source_id.service", "=", META_ADS_SERVICE),
                    ("webhook_page_id", "=", dispatch.page_id.id),
                    ("meta_app_id", "=", dispatch.delivery_id.app_id.id),
                    ("external_form_id", "=", form_id),
                ]
            )
        )
        if len(routes) != 1:
            return self.env["marketing.center.meta.lead.submission"]
        route = routes.ensure_one()
        timestamp = value.get("created_time")
        hint_created_at = (
            datetime.datetime.utcfromtimestamp(timestamp)
            if isinstance(timestamp, int)
            and not isinstance(timestamp, bool)
            and 0 <= timestamp <= 2**31 - 1
            else False
        )
        service = dispatch.env["marketing.center.meta.lead.service"]
        return service._find_or_create_submission(
            route,
            leadgen_id=leadgen_id,
            origin="webhook",
            page_id=page_id,
            form_id=form_id,
            ad_id=str(value.get("ad_id") or ""),
            legacy_adgroup_id=str(value.get("adgroup_id") or ""),
            hint_created_at=hint_created_at,
            hint_sha256=item.event_sha256,
            dispatch=dispatch,
        )

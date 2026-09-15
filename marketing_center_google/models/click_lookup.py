import datetime
import json
import logging
import uuid

from psycopg2.errors import SerializationFailure

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.google_api_base.services.errors import GoogleApiError
from odoo.addons.marketing_center_base.services.dto import (
    MarketingIdentifierDTO,
    MarketingTouchpointDTO,
    PrivacySnapshotDTO,
)
from odoo.addons.marketing_center_base.services.serialization import (
    acquire_advisory_xact_lock,
)
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.adapter import GoogleMarketingReadAdapter
from ..services.catalog import GOOGLE_ADS_SERVICE
from ..services.click import click_local_date
from .sync_common import GoogleDeferredRetry

_TOKEN = object()
_ACTIVE = {"planned", "queued", "running"}
_EMPTY_RETRY_SECONDS = (900, 3600, 21600, 86400)
_logger = logging.getLogger(__name__)


class MarketingGoogleClickLookup(models.Model):
    _name = "marketing.center.google.click.lookup"
    _description = "Google Click Acquisition Lookup"
    _order = "id desc"
    _rec_name = "public_ref"
    _check_company_auto = True

    public_ref = fields.Char(
        default=lambda self: str(uuid.uuid4()), readonly=True, required=True, index=True
    )
    company_id = fields.Many2one(
        "res.company", required=True, readonly=True, index=True
    )
    canonical_key = fields.Char(required=True, readonly=True, index=True)
    touchpoint_id = fields.Many2one(
        "marketing.attribution.touchpoint",
        required=True,
        readonly=True,
        check_company=True,
        ondelete="restrict",
    )
    identifier_id = fields.Many2one(
        "marketing.attribution.identifier",
        required=True,
        readonly=True,
        check_company=True,
        ondelete="restrict",
    )
    source_id = fields.Many2one(
        "marketing.center.source",
        readonly=True,
        check_company=True,
        ondelete="restrict",
    )
    sync_run_id = fields.Many2one(
        "marketing.center.sync.run",
        readonly=True,
        check_company=True,
        ondelete="restrict",
    )
    run_state = fields.Selection(related="sync_run_id.state", readonly=True)
    enriched_touchpoint_id = fields.Many2one(
        "marketing.attribution.touchpoint",
        readonly=True,
        check_company=True,
        ondelete="restrict",
    )
    state = fields.Selection(
        [
            (x, label)
            for x, label in (
                ("pending", "Pending"),
                ("waiting", "Waiting for Google"),
                ("found", "Found"),
                ("no_match", "No match after retries"),
                ("ambiguous", "Ambiguous account"),
                ("blocked", "Blocked"),
                ("stale", "Configuration changed"),
                ("expired", "Outside query window"),
                ("error", "Lookup failed"),
            )
        ],
        default="pending",
        required=True,
        readonly=True,
        index=True,
    )
    reason = fields.Char(readonly=True)
    attempt_count = fields.Integer(default=0, readonly=True)
    source_candidate_count = fields.Integer(default=0, readonly=True)
    local_date = fields.Date(readonly=True)
    report_timezone = fields.Char(readonly=True)
    last_attempted_at = fields.Datetime(readonly=True)
    next_attempt_at = fields.Datetime(readonly=True)
    result_json = fields.Json(
        readonly=True, groups="marketing_center_base.group_marketing_center_admin"
    )

    confirmed_campaign = fields.Char(
        string="Campanha", compute="_compute_confirmed_details",
        groups="marketing_center_base.group_marketing_center_admin",
    )
    confirmed_ad_group = fields.Char(
        string="Grupo de anúncios", compute="_compute_confirmed_details",
        groups="marketing_center_base.group_marketing_center_admin",
    )
    confirmed_network = fields.Char(
        string="Rede", compute="_compute_confirmed_details",
        groups="marketing_center_base.group_marketing_center_admin",
    )
    confirmed_device = fields.Char(
        string="Dispositivo", compute="_compute_confirmed_details",
        groups="marketing_center_base.group_marketing_center_admin",
    )
    confirmed_keyword = fields.Char(
        string="Palavra-chave do anúncio", compute="_compute_confirmed_details",
        groups="marketing_center_base.group_marketing_center_admin",
    )
    confirmed_match_type = fields.Char(
        string="Correspondência", compute="_compute_confirmed_details",
        groups="marketing_center_base.group_marketing_center_admin",
    )
    confirmed_response_text = fields.Text(
        string="Resposta técnica", compute="_compute_confirmed_details",
        groups="marketing_center_base.group_marketing_center_admin",
    )

    @api.depends("result_json")
    def _compute_confirmed_details(self):
        networks = {
            "SEARCH": _("Pesquisa Google"),
            "SEARCH_PARTNERS": _("Parceiros de pesquisa"),
            "CONTENT": _("Rede de Display"),
            "YOUTUBE_SEARCH": _("Pesquisa do YouTube"),
            "YOUTUBE_WATCH": _("Vídeos do YouTube"),
        }
        devices = {
            "DESKTOP": _("Computador"), "MOBILE": _("Celular"),
            "TABLET": _("Tablet"), "CONNECTED_TV": _("TV conectada"),
        }
        matches = {
            "BROAD": _("Ampla"), "PHRASE": _("Frase"), "EXACT": _("Exata"),
        }
        for lookup in self:
            result = lookup.result_json if isinstance(lookup.result_json, dict) else {}
            details = result.get("details")
            details = details if isinstance(details, dict) else {}
            def text(key):
                value = details.get(key)
                return value if isinstance(value, str) else False
            lookup.confirmed_campaign = text("campaign_name")
            lookup.confirmed_ad_group = text("ad_group_name")
            lookup.confirmed_network = networks.get(text("network"), text("network"))
            lookup.confirmed_device = devices.get(text("device"), text("device"))
            lookup.confirmed_keyword = text("keyword_text")
            lookup.confirmed_match_type = matches.get(
                text("keyword_match_type"), text("keyword_match_type")
            )
            lookup.confirmed_response_text = (
                json.dumps(result, ensure_ascii=False, indent=2) if result else False
            )

    _sql_constraints = [
        (
            "canonical_unique",
            "unique(company_id, canonical_key)",
            "A click occurrence already has a lookup.",
        ),
        (
            "public_ref_unique",
            "unique(public_ref)",
            "Click lookup references must be unique.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        self._require_internal()
        return super().create(vals_list)

    def write(self, values):
        self._require_internal()
        return super().write(values)

    def unlink(self):
        raise AccessError(_("Google click lookup evidence cannot be deleted."))

    def _require_internal(self):
        if self.env.context.get("marketing_google_click_token") is not _TOKEN:
            raise AccessError(
                _("Google click lookups are maintained by their service.")
            )

    def action_retry(self):
        self.check_access_rights("read")
        self.check_access_rule("read")
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_admin"
        ):
            raise AccessError(
                _("Only marketing administrators can retry click lookups.")
            )
        return self.env["marketing.center.google.click.service"]._reconcile_touchpoints(
            self.touchpoint_id.ids
        )


class MarketingSourceGoogleClick(models.Model):
    _inherit = "marketing.center.source"

    google_click_lookup_enabled = fields.Boolean(
        string="Resolve captured Google clicks",
        default=False,
        groups="marketing_center_base.group_marketing_center_admin",
        help="Opt in to exact GCLID lookups for new eligible acquisition evidence. Historical processing requires an explicit scoped retry.",
    )

    def write(self, values):
        if {
            "google_click_lookup_enabled",
            "active",
            "state",
            "read_enabled",
        }.intersection(values):
            for company in self.company_id.sorted("id"):
                acquire_advisory_xact_lock(
                    self.env.cr,
                    "marketing_google_click_sources:%s" % company.id,
                    "Concurrent click source selection needs a fresh snapshot",
                )
        if "google_click_lookup_enabled" in values:
            if not self.env.user.has_group(
                "marketing_center_base.group_marketing_center_admin"
            ):
                raise AccessError(
                    _("Only marketing administrators can configure click lookup.")
                )
            # The neutral source revision fences every active provider job. Force
            # its existing revision path; no parallel revision counter is needed.
            for source in self:
                scoped = dict(values)
                scoped.setdefault("read_enabled", source.read_enabled)
                super(MarketingSourceGoogleClick, source).write(scoped)
            return True
        return super().write(values)

    @api.constrains("google_click_lookup_enabled", "service")
    def _check_click_service(self):
        for company in self.company_id.sorted("id"):
            acquire_advisory_xact_lock(
                self.env.cr,
                "marketing_google_click_sources:%s" % company.id,
                "Concurrent click source selection needs a fresh snapshot",
            )
        if any(
            s.google_click_lookup_enabled and s.service != GOOGLE_ADS_SERVICE
            for s in self
        ):
            raise ValidationError(
                _("Click lookup is available only for Google Ads sources.")
            )

    @api.model
    def _identity_evidence_registry(self):
        return super()._identity_evidence_registry() + (
            (
                "marketing.center.google.click.lookup",
                "source_id",
                (),
            ),
        )


class MarketingSyncRunGoogleClick(models.Model):
    _inherit = "marketing.center.sync.run"

    def _job_lookup_google_click(self, expected_attempt, quota_attempt=0):
        self.ensure_one()
        return self.env["marketing.center.google.click.service"]._execute(
            self,
            expected_attempt=expected_attempt,
            quota_attempt=quota_attempt,
        )


class MarketingGoogleClickService(models.AbstractModel):
    _name = "marketing.center.google.click.service"
    _inherit = "marketing.center.google.sync.service"
    _description = "Exact Google Click Enrichment Service"

    @api.model
    def _lookups(self):
        return (
            self.env["marketing.center.google.click.lookup"]
            .sudo()
            .with_context(marketing_google_click_token=_TOKEN)
        )

    @api.model
    def _source_candidates(self, company):
        acquire_advisory_xact_lock(
            self.env.cr,
            "marketing_google_click_sources:%s" % company.id,
            "Concurrent click source selection needs a fresh snapshot",
        )
        return (
            self.env["marketing.center.source"]
            .sudo()
            .search(
                [
                    ("company_id", "=", company.id),
                    ("service", "=", GOOGLE_ADS_SERVICE),
                    ("google_click_lookup_enabled", "=", True),
                    ("active", "=", True),
                    ("state", "=", "active"),
                    ("read_enabled", "=", True),
                ],
                limit=3,
            )
        )

    @api.model
    def _effective(self, lookup):
        return (
            self.env["marketing.attribution.effective.touchpoint"]
            .sudo()
            .search(
                [
                    ("company_id", "=", lookup.company_id.id),
                    ("canonical_key", "=", lookup.canonical_key),
                ],
                limit=1,
            )
            .touchpoint_id
        )

    @api.model
    def _schedule_touchpoint(self, touchpoint, *, explicit=False):
        touchpoint.ensure_one()
        if touchpoint.company_id not in self.env.companies:
            raise AccessError(_("The touchpoint belongs to another company."))
        if touchpoint.privacy_erased_at or touchpoint.consent_state == "denied":
            return False
        identifiers = touchpoint.identifier_ids.filtered(
            lambda i: i.namespace == "google.gclid"
            and i.role == "click"
            and i.value_ref
            and not i.erased_at
        )
        if len(identifiers) != 1:
            return False
        acquire_advisory_xact_lock(
            self.env.cr,
            "marketing_attribution:%s:%s"
            % (touchpoint.company_id.id, touchpoint.canonical_key),
            "Concurrent click evidence needs a fresh snapshot",
        )
        sources = self._source_candidates(touchpoint.company_id)
        if not sources and not explicit:
            return False
        acquire_advisory_xact_lock(
            self.env.cr,
            "marketing_google_click:%s:%s"
            % (touchpoint.company_id.id, touchpoint.canonical_key),
            "Concurrent click lookup needs a fresh snapshot",
        )
        lookup = self._lookups().search(
            [
                ("company_id", "=", touchpoint.company_id.id),
                ("canonical_key", "=", touchpoint.canonical_key),
            ],
            limit=1,
        )
        if lookup and (lookup.state == "found" or lookup.sync_run_id.state in _ACTIVE):
            return lookup
        if lookup and not explicit:
            return lookup
        if not lookup:
            lookup = self._lookups().create(
                {
                    "company_id": touchpoint.company_id.id,
                    "canonical_key": touchpoint.canonical_key,
                    "touchpoint_id": touchpoint.id,
                    "identifier_id": identifiers.id,
                }
            )
        lookup.write({"source_candidate_count": len(sources)})
        if len(sources) != 1:
            lookup.write(
                {
                    "state": "ambiguous" if sources else "blocked",
                    "reason": "multiple_sources" if sources else "lookup_disabled",
                }
            )
            return lookup
        source = sources
        try:
            local_date = click_local_date(
                touchpoint.occurred_at, source.timezone, now=fields.Datetime.now()
            )
        except GoogleApiError:
            lookup.write({"state": "expired", "reason": "outside_90_day_window"})
            return lookup
        try:
            connection = self._reader_connection(source)
        except ValidationError:
            lookup.write({"state": "blocked", "reason": "reader_unavailable"})
            return lookup
        run = self.env["marketing.center.sync.service"]._plan_run(
            source.company_id,
            source,
            connection,
            sync_kind="leads",
            entity_type="google_click_lookup",
            scope_ref="click:%s" % lookup.public_ref,
            trigger_kind="manual" if explicit else "webhook",
            trigger_ref="lookup:%s:%s" % (lookup.public_ref, uuid.uuid4()),
            reporting_context={
                "contract": "google.click.v1",
                "lookup_ref": lookup.public_ref,
                "local_date": local_date.isoformat(),
            },
            report_timezone=source.timezone,
        )
        lookup.write(
            {
                "source_id": source.id,
                "sync_run_id": run.id,
                "state": "pending",
                "reason": "queued",
                "attempt_count": 0,
                "local_date": local_date,
                "report_timezone": source.timezone,
                "next_attempt_at": False,
            }
        )
        self._enqueue_click(run, 0)
        return lookup

    @api.model
    def _reconcile_touchpoints(self, touchpoint_ids):
        if (
            not isinstance(touchpoint_ids, (list, tuple))
            or not 1 <= len(touchpoint_ids) <= 200
            or any(type(i) is not int or i <= 0 for i in touchpoint_ids)
        ):
            raise ValidationError(
                _("Explicit click reconciliation requires 1 to 200 touchpoint IDs.")
            )
        points = (
            self.env["marketing.attribution.touchpoint"].browse(touchpoint_ids).exists()
        )
        points.check_access_rights("read")
        points.check_access_rule("read")
        result = []
        for point in points:
            lookup = self._schedule_touchpoint(point, explicit=True)
            if lookup:
                result.append(lookup.id)
        return result

    @api.model
    def _lookup_for_run(self, run):
        if (
            run.company_id not in self.env.companies
            or run.entity_type != "google_click_lookup"
            or run.sync_kind != "leads"
        ):
            raise ValidationError(_("The Google click run is invalid."))
        lookup = self._lookups().search([("sync_run_id", "=", run.id)], limit=1)
        if not lookup or lookup.source_id != run.source_id:
            raise ValidationError(_("The Google click run has no matching lookup."))
        return lookup

    @api.model
    def _enqueue_click(
        self, run, expected_attempt, *, eta_seconds=0, resume_key="", quota_attempt=0
    ):
        lookup = self._lookup_for_run(run)
        delayed = run.with_delay(
            identity_key="marketing_google:click:%s:%s:%s:%s"
            % (run.public_ref, expected_attempt, quota_attempt, resume_key),
            eta=eta_seconds or None,
            max_retries=8,
            priority=45,
            description="Resolve Google click acquisition %s" % lookup.public_ref,
        )._job_lookup_google_click(expected_attempt, quota_attempt)
        values = {
            "queue_job_uuid": str(delayed.uuid),
            "deferred_until": self._run_deferred_until(eta_seconds),
        }
        sync = self.env["marketing.center.sync.service"]
        if run.state == "planned":
            sync._transition(run, "queued", values)
        else:
            sync._write_run(run, values)
        lookup.write({"next_attempt_at": self._run_deferred_until(eta_seconds)})
        return delayed

    @api.model
    def _execute(self, run, *, expected_attempt, quota_attempt=0):
        self._quota_attempt(quota_attempt)
        try:
            with self.env.cr.savepoint():
                return self._execute_current(run, expected_attempt)
        except GoogleDeferredRetry as retry:
            return self._persist_quota_retry(
                run,
                retry,
                expected_cursor_sequence=expected_attempt,
                quota_attempt=quota_attempt,
                enqueue_method="_enqueue_click",
            )
        except (RetryableJobError, SerializationFailure):
            raise
        except Exception:
            # Never attach provider exceptions, raw rows, query or click IDs to
            # OCA job errors. Only the bounded existing retry policy is exposed.
            job_uuid = self._current_job_uuid(run)
            if not job_uuid:
                return {"orphan": True}
            return self._unexpected_failure(run, job_uuid, "click lookup")

    @api.model
    def _execute_current(self, run, expected_attempt):
        lookup = self._lookup_for_run(run)
        if run.state not in _ACTIVE:
            return {"terminal": run.state}
        job_uuid = self._current_job_uuid(run)
        if not job_uuid:
            return {"orphan": True}
        if (
            type(expected_attempt) is not int
            or expected_attempt != lookup.attempt_count
        ):
            return {"stale_attempt": True}
        point = self._effective(lookup)
        # The vault serializes endpoint policy/event retention and canonical
        # evidence before the source/profile locks, matching ingress/erasure.
        gclid = self._protected_input(lookup, point)
        candidates = self._source_candidates(lookup.company_id)
        profile = self._current_profile(run.connection_id, strict=False)
        if (
            not profile
            or not self._preflight_current(run, profile)
            or not self._lock_execution_fences(run, job_uuid, profile)
        ):
            lookup.write({"state": "stale", "reason": "configuration_changed"})
            return self._mark_stale(run, job_uuid)
        if candidates != lookup.source_id:
            return self._finish_lookup(
                run,
                lookup,
                "ambiguous" if len(candidates) > 1 else "blocked",
                "source_selection_changed",
            )
        if not gclid:
            return self._finish_lookup(
                run, lookup, "blocked", "privacy_or_identifier_unavailable"
            )
        try:
            local_date = click_local_date(
                point.occurred_at, lookup.source_id.timezone, now=fields.Datetime.now()
            )
        except GoogleApiError:
            return self._finish_lookup(run, lookup, "expired", "outside_90_day_window")
        if (
            local_date != lookup.local_date
            or lookup.report_timezone != lookup.source_id.timezone
        ):
            return self._finish_lookup(run, lookup, "stale", "event_date_changed")
        self._cooldown_preflight(run.connection_id)
        try:
            match = GoogleMarketingReadAdapter(
                profile,
                expected_profile_revision=run.profile_revision,
                expected_identity_revision=run.connection_id.google_identity_revision,
                login_customer_id=run.connection_id.google_login_customer_id or "",
            ).fetch_click(
                run.source_id.external_account_id,
                gclid,
                occurred_at=point.occurred_at,
                report_timezone=lookup.report_timezone,
                now=fields.Datetime.now(),
            )
        except GoogleApiError as error:
            lookup.write({"state": "error", "reason": "provider_error"})
            return self._provider_error(
                run, job_uuid, profile, error, "Google click lookup failed safely."
            )
        if (
            not self._preflight_current(run, profile)
            or self._source_candidates(lookup.company_id) != lookup.source_id
            or not self._protected_input(lookup, point)
        ):
            return self._finish_lookup(
                run, lookup, "stale", "configuration_or_privacy_changed"
            )
        lookup.write(
            {
                "attempt_count": lookup.attempt_count + 1,
                "last_attempted_at": fields.Datetime.now(),
            }
        )
        if match is None:
            if lookup.attempt_count <= len(_EMPTY_RETRY_SECONDS):
                delay = _EMPTY_RETRY_SECONDS[lookup.attempt_count - 1]
                lookup.write({"state": "waiting", "reason": "no_match_yet"})
                self._enqueue_click(run, lookup.attempt_count, eta_seconds=delay)
                return {"waiting": True}
            return self._finish_lookup(
                run, lookup, "no_match", "no_match_after_retries"
            )
        enriched = self._enrich(point, lookup, match)
        if enriched.disposition not in {"accepted", "enriched", "revised", "duplicate"}:
            return self._finish_lookup(
                run, lookup, "blocked", "enrichment_not_accepted"
            )
        lookup.write(
            {
                "enriched_touchpoint_id": enriched.touchpoint_id,
                "result_json": {"assets": match.assets, "details": match.details},
            }
        )
        self._mark_connection_success(run.connection_id)
        return self._finish_lookup(run, lookup, "found", "exact_click_match")

    @api.model
    def _protected_input(self, lookup, point):
        if not point or point.privacy_erased_at or point.consent_state == "denied":
            return None
        identifier = lookup.identifier_id
        if identifier.erased_at or (
            identifier.retain_until and identifier.retain_until < fields.Date.today()
        ):
            return None
        current = point.identifier_ids.filtered(
            lambda i: i.namespace == "google.gclid"
            and i.role == "click"
            and i.value_ref == identifier.value_ref
            and i.comparison_hash == identifier.comparison_hash
            and not i.erased_at
        )
        if len(current) != 1:
            return None
        if "marketing.web.ingress.service" not in self.env.registry.models:
            return None
        vault = self.env["marketing.web.ingress.service"]
        if not hasattr(vault, "_google_click_input"):
            return None
        return vault._google_click_input(current, lock=True)

    @api.model
    def _finish_lookup(self, run, lookup, state, reason):
        lookup.write({"state": state, "reason": reason, "next_attempt_at": False})
        sync = self.env["marketing.center.sync.service"]
        if run.state in {"planned", "queued"}:
            sync._transition(run, "running", {"started_at": fields.Datetime.now()})
        sync._transition(
            run,
            "succeeded" if state in {"found", "no_match"} else "cancelled",
            {"finished_at": fields.Datetime.now()},
        )
        return {"state": state}

    @api.model
    def _enrich(self, point, lookup, match):
        assets = dict(point.asset_refs_json or {})
        if any(
            key in assets and assets[key] != value
            for key, value in match.assets.items()
        ):
            raise ValidationError(
                _("Google click assets conflict with current evidence.")
            )
        assets.update(match.assets)
        extensions = dict(point.extensions_json or {})
        extensions["google.click_lookup"] = {
            "lookup_ref": lookup.public_ref,
            "local_date": lookup.local_date.isoformat(),
            "timezone": lookup.report_timezone,
            "source_ref": lookup.source_id.public_ref,
            "method": "click_view_exact",
            **match.details,
        }
        dto = MarketingTouchpointDTO(
            source_system=point.source_system,
            source_scope_ref=point.source_scope_ref,
            source_occurrence_ref=point.source_occurrence_ref,
            source_evidence_ref="google.click.lookup:%s" % lookup.public_ref,
            source_schema_version=point.source_schema_version or "",
            occurred_at=point.occurred_at,
            observed_at=fields.Datetime.now(),
            platform=point.platform,
            channel=point.channel,
            network=point.network or "",
            touchpoint_type=point.touchpoint_type,
            evidence_level="provider_asserted",
            revision_kind="enrichment",
            landing_url=point.landing_url or "",
            referrer_url=point.referrer_url or "",
            utm={
                name: point["utm_%s" % name]
                for name in ("source", "medium", "campaign", "content", "term")
                if point["utm_%s" % name]
            },
            asset_refs=assets,
            extensions=extensions,
            mapping_version=point.mapping_version,
            identifiers=tuple(
                MarketingIdentifierDTO(
                    namespace=i.namespace,
                    role=i.role,
                    comparison_hash=i.comparison_hash,
                    masked_value=i.masked_value or "",
                    value_ref=i.value_ref or "",
                    source_field=i.source_field or "",
                    purpose=i.purpose,
                    retain_until=i.retain_until or None,
                )
                for i in point.identifier_ids
                if not i.erased_at
            ),
            privacy=PrivacySnapshotDTO(
                policy_version=point.policy_version or "",
                notice_version=point.notice_version or "",
                legal_basis_code=point.legal_basis_code or "",
                consent_state=point.consent_state,
                decision_source=point.privacy_decision_source or "",
                decided_at=point.privacy_decided_at or None,
            ),
        )
        return self.env["marketing.attribution.service"]._ingest_touchpoint(
            point.company_id, dto
        )


class MarketingAttributionGoogleClick(models.AbstractModel):
    _inherit = "marketing.attribution.service"

    @api.model
    def _ingest_touchpoint(self, company, payload):
        result = super()._ingest_touchpoint(company, payload)
        point = (
            self.env["marketing.attribution.touchpoint"]
            .sudo()
            .browse(result.touchpoint_id)
        )
        # Enrichment cannot enqueue itself. Existing completed lookups also fence
        # replays of the original web occurrence.
        if (point.extensions_json or {}).get("google.click_lookup"):
            return result
        try:
            with self.env.cr.savepoint():
                self.env["marketing.center.google.click.service"]._schedule_touchpoint(
                    point
                )
        except SerializationFailure:
            raise
        except Exception:
            _logger.error(
                "Google click scheduling failed for touchpoint %s; explicit reconciliation is available",
                point.public_ref,
            )
        return result


class MarketingTouchpointGoogleClickRetention(models.Model):
    _inherit = "marketing.attribution.touchpoint"

    def _erase_private_values(self, *, token, now):
        result = super()._erase_private_values(token=token, now=now)
        for point in self:
            lookups = (
                self.env["marketing.center.google.click.service"]
                ._lookups()
                .search(
                    [
                        ("company_id", "=", point.company_id.id),
                        ("canonical_key", "=", point.canonical_key),
                    ]
                )
            )
            lookups.write(
                {
                    "result_json": {},
                    "state": "blocked",
                    "reason": "privacy_erased",
                    "next_attempt_at": False,
                }
            )
        return result

import datetime
import logging

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.google_api_base.services.errors import (
    GoogleApiPausedError,
    GoogleApiRateLimitError,
    GoogleApiTransientError,
)
from odoo.addons.marketing_center_base.services.tokens import (
    MARKETING_CONFIGURATION_RUNTIME_TOKEN,
)
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.catalog import GOOGLE_ADAPTER_KEY, GOOGLE_ADS_SERVICE
from ..services.tokens import MARKETING_GOOGLE_CONNECTION_TOKEN

_ACTIVE_RUN_STATES = {"planned", "queued", "running"}
_ATTEMPT_CEILING = 8
_QUOTA_RESCHEDULE_CEILING = 8
_SAFE_ERROR_CLASSES = {
    "limit_exceeded",
    "paused",
    "permission_denied",
    "permanent",
    "rate_limited",
    "transient",
}

_logger = logging.getLogger(__name__)


class GoogleDeferredRetry(Exception):
    """Commit a provider-requested wait outside the page savepoint."""

    def __init__(self, seconds, *, classification):
        super().__init__("Google Ads deferred retry")
        self.seconds = max(1, min(int(seconds), 86_400))
        self.classification = (
            classification
            if classification in {"rate_limited", "transient"}
            else "transient"
        )


class GoogleQuotaRetry(GoogleDeferredRetry):
    """Commit a quota wait and its connection health state."""

    def __init__(self, seconds):
        super().__init__(seconds, classification="rate_limited")


class MarketingCenterGoogleSyncService(models.AbstractModel):
    _name = "marketing.center.google.sync.service"
    _description = "Marketing Center Google Synchronization Fences"

    @api.model
    def _scheduled_sources(self, source_ids=None, *, limit=25, scheduler_key):
        limit = self._bound(limit, "source", 200)
        domain = [
            ("active", "=", True),
            ("service", "=", GOOGLE_ADS_SERVICE),
            ("state", "=", "active"),
            ("read_enabled", "=", True),
        ]
        if source_ids is not None:
            if not isinstance(source_ids, (list, tuple)) or any(
                not isinstance(source_id, int)
                or isinstance(source_id, bool)
                or source_id <= 0
                for source_id in source_ids
            ):
                raise ValidationError(_("The scheduled source IDs are invalid."))
            domain.append(("id", "in", list(source_ids)))
        source_model = self.env["marketing.center.source"].sudo()
        if source_ids is not None:
            return source_model.search(domain, order="id", limit=limit)
        return source_model._fair_scheduler_batch(
            domain,
            cursor_key="google.%s" % scheduler_key,
            limit=limit,
        )

    @api.model
    def _reader_connection(self, source):
        source = source.sudo().exists()
        if (
            not source
            or len(source) != 1
            or source.company_id not in self.env.companies
            or source.service != GOOGLE_ADS_SERVICE
        ):
            raise ValidationError(_("A valid Google Ads source is required."))
        connection = (
            self.env["marketing.center.connection"]
            .sudo()
            .search(
                [
                    ("source_id", "=", source.id),
                    ("adapter_key", "=", GOOGLE_ADAPTER_KEY),
                    ("purpose", "=", "reader"),
                    ("active", "=", True),
                    ("state", "=", "ready"),
                ],
                limit=1,
            )
        )
        if not connection:
            raise ValidationError(_("The Google Ads reader connection is not ready."))
        self._current_profile(connection)
        return connection

    @api.model
    def _current_profile(self, connection, *, strict=True):
        profile = connection.sudo().google_profile_id.exists()
        identity = profile.google_identity_id if profile else False
        valid = bool(
            profile
            and len(profile) == 1
            and profile.active
            and identity.active
            and profile.company_id == connection.company_id == identity.company_id
            and profile.public_ref == connection.profile_public_ref
            and profile.profile_revision == connection.profile_revision
            and profile.identity_revision == connection.google_identity_revision
            and identity.revision == profile.identity_revision
        )
        if not valid:
            if strict:
                raise ValidationError(
                    _("The Google Ads reader profile is not current.")
                )
            return self.env["marketing.center.google.profile"]
        return profile

    @api.model
    def _lock_execution_fences(self, run, job_uuid, profile):
        if not self._lock_current_job(run, job_uuid):
            return False
        google_service = self.env["marketing.center.google.service"]
        if not google_service._lock_current_profile(
            profile,
            run.profile_revision,
            run.connection_id.google_identity_revision,
        ):
            return False
        return self.env["marketing.center.sync.service"]._validate_fencing(run)

    @api.model
    def _preflight_current(self, run, profile):
        source = run.source_id
        connection = run.connection_id
        identity = profile.google_identity_id
        return bool(
            run.state in _ACTIVE_RUN_STATES
            and source.active
            and source.read_enabled
            and source.state == "active"
            and source.configuration_revision == run.source_revision
            and connection.active
            and connection.state == "ready"
            and connection.purpose == "reader"
            and connection.source_id == source
            and connection.adapter_key == run.adapter_key == GOOGLE_ADAPTER_KEY
            and connection.binding_revision == run.binding_revision
            and connection.profile_revision == run.profile_revision
            and profile.profile_revision == run.profile_revision
            and connection.google_identity_revision == profile.identity_revision
            and identity.revision == profile.identity_revision
            and identity.active
        )

    @api.model
    def _restart_cursor(self, run):
        if run.state != "planned":
            raise ValidationError(
                _("A Google synchronization can restart only before queueing.")
            )
        profile = self._current_profile(run.connection_id)
        sync_service = self.env["marketing.center.sync.service"]
        if not self._preflight_current(
            run, profile
        ) or not sync_service._validate_fencing(run):
            raise ValidationError(_("Google synchronization configuration changed."))
        cursor = sync_service._locked_cursor(run)
        if not any(
            (
                cursor.cursor_value,
                cursor.cursor_digest,
                cursor.provider_job_ref,
                cursor.watermark,
            )
        ):
            return cursor.cursor_sequence
        sync_service._write_cursor(
            cursor,
            {
                "cursor_value": False,
                "cursor_digest": False,
                "provider_job_ref": False,
                "watermark": False,
                "cursor_sequence": cursor.cursor_sequence + 1,
                "last_advanced_at": fields.Datetime.now(),
            },
        )
        return cursor.cursor_sequence

    @api.model
    def _cursor_snapshot(self, run):
        cursor = (
            self.env["marketing.center.sync.cursor"]
            .sudo()
            .search(
                [
                    ("source_id", "=", run.source_id.id),
                    ("adapter_key", "=", run.adapter_key),
                    ("cursor_kind", "=", run.sync_kind),
                    ("entity_type", "=", run.entity_type or False),
                    ("grain", "=", run.grain or False),
                    ("scope_ref", "=", run.scope_ref),
                    ("reporting_context_hash", "=", run.reporting_context_hash),
                    ("window_key", "=", run.window_key),
                ],
                limit=1,
            )
        )
        return (
            (cursor.cursor_sequence, cursor.cursor_value or "") if cursor else (0, "")
        )

    @api.model
    def _reschedule_current_cursor(self, run, job_uuid, *, enqueue_method):
        """Atomically replace a stale page argument with the current cursor.

        The run UUID remains the execution fence: only its currently registered
        job may schedule the replacement.  Locking both the run and canonical
        cursor prevents a concurrent advance from being lost between detection
        and enqueueing.
        """

        if not self._lock_current_job(run, job_uuid):
            return {"orphan": True}
        profile = self._current_profile(run.connection_id, strict=False)
        if not profile:
            return self._mark_stale(run, job_uuid)
        if not self._lock_execution_fences(run, job_uuid, profile):
            return self._mark_stale(run, job_uuid)
        cursor = self.env["marketing.center.sync.service"]._locked_cursor(run)
        cursor_sequence = cursor.cursor_sequence
        getattr(self, enqueue_method)(run, cursor_sequence)
        return {
            "cursor_changed": True,
            "rescheduled": True,
            "cursor_sequence": cursor_sequence,
        }

    @api.model
    def _apply_pages(
        self,
        run,
        job_uuid,
        pages,
        expected_cursor_sequence,
        *,
        apply_method,
        enqueue_method,
    ):
        profile = self._current_profile(run.connection_id, strict=False)
        if not profile or not self._lock_execution_fences(run, job_uuid, profile):
            return self._mark_stale(run, job_uuid)
        if not isinstance(pages, tuple) or not pages:
            return self._finish_failure(
                run,
                job_uuid,
                classification="permanent",
                summary="Google Ads returned no bounded synchronization page.",
            )
        sync_service = self.env["marketing.center.sync.service"]
        try:
            with self.env.cr.savepoint():
                sequence = expected_cursor_sequence
                for page in pages:
                    getattr(sync_service, apply_method)(
                        run,
                        page,
                        expected_cursor_sequence=sequence,
                    )
                    sequence += 1
                run.invalidate_recordset(["state", "page_count"])
                if pages[-1].has_more and run.state == "running":
                    getattr(self, enqueue_method)(run, sequence)
        except ValidationError:
            run.invalidate_recordset(["state", "page_count", "queue_job_uuid"])
            return self._finish_failure(
                run,
                job_uuid,
                classification="permanent",
                summary="Google Ads page contract could not be applied.",
            )
        self._mark_connection_success(run.connection_id)
        run.invalidate_recordset(["state", "page_count"])
        return {
            "state": run.state,
            "received": sum(len(page.items) for page in pages),
            "has_more": pages[-1].has_more,
        }

    @api.model
    def _provider_error(self, run, job_uuid, profile, error, summary):
        if not self._lock_execution_fences(run, job_uuid, profile):
            return self._mark_stale(run, job_uuid)
        if isinstance(error, (GoogleApiRateLimitError, GoogleApiTransientError)):
            return self._retry_or_finish(run, job_uuid, error)
        if isinstance(error, GoogleApiPausedError):
            return self._finish_paused(run, job_uuid, profile, error)
        return self._finish_failure(
            run,
            job_uuid,
            classification=getattr(error, "classification", "permanent"),
            summary=summary,
        )

    @api.model
    def _retry_or_finish(self, run, job_uuid, error):
        retry_after = getattr(error, "retry_after_seconds", 0) or 0
        if isinstance(error, GoogleApiRateLimitError):
            raise GoogleQuotaRetry(retry_after or 60) from None
        if retry_after:
            raise GoogleDeferredRetry(
                retry_after,
                classification="transient",
            ) from None
        if self._job_attempt(job_uuid) < _ATTEMPT_CEILING:
            raise RetryableJobError(
                "Google Ads is temporarily unavailable",
                seconds=retry_after or None,
            ) from None
        return self._finish_failure(
            run,
            job_uuid,
            classification=getattr(error, "classification", "transient"),
            summary="Google Ads exhausted its bounded retry policy.",
        )

    @api.model
    def _persist_quota_retry(
        self,
        run,
        quota_retry,
        *,
        expected_cursor_sequence,
        quota_attempt,
        enqueue_method,
    ):
        """Commit cooldown with an ETA successor instead of rolling it back."""

        run = run.sudo().exists()
        if not run or len(run) != 1:
            return {"missing": True}
        run.invalidate_recordset(["queue_job_uuid", "state"])
        job_uuid = self._current_job_uuid(run)
        if not job_uuid:
            return {"orphan": True}
        profile = self._current_profile(run.connection_id, strict=False)
        if not profile or not self._lock_execution_fences(run, job_uuid, profile):
            return self._mark_stale(run, job_uuid)
        self._mark_connection_cooldown(
            run.connection_id,
            quota_retry.seconds,
            classification=quota_retry.classification,
        )
        quota_attempt = self._quota_attempt(quota_attempt)
        if quota_attempt >= _QUOTA_RESCHEDULE_CEILING:
            return self._finish_failure(
                run,
                job_uuid,
                classification=quota_retry.classification,
                summary="Google Ads exhausted its bounded deferred retry policy.",
            )
        getattr(self, enqueue_method)(
            run,
            expected_cursor_sequence,
            eta_seconds=quota_retry.seconds,
            resume_key=job_uuid,
            quota_attempt=quota_attempt + 1,
        )
        return {
            "rescheduled": True,
            "retry_after": quota_retry.seconds,
            "quota_attempt": quota_attempt + 1,
        }

    @api.model
    def _quota_attempt(self, value):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= _QUOTA_RESCHEDULE_CEILING
        ):
            raise ValidationError(_("The Google quota retry count is invalid."))
        return value

    @api.model
    def _quota_delay(self, value):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= 86_400
        ):
            raise ValidationError(_("The Google quota retry delay is invalid."))
        return value

    @api.model
    def _cooldown_preflight(self, connection):
        if not connection.cooldown_until:
            return
        now = fields.Datetime.now()
        if connection.cooldown_until <= now:
            return
        seconds = max(
            1,
            min(int((connection.cooldown_until - now).total_seconds()) + 1, 86_400),
        )
        # The ordinary queue_job retry exception rolls the Odoo transaction
        # back, so the core watchdog could not distinguish this intentional ETA
        # from an abandoned run.  The quota path commits a bounded successor and
        # its ``deferred_until`` fence instead.
        classification = (
            connection.last_health_error_class
            if connection.last_health_error_class in {"rate_limited", "transient"}
            else "rate_limited"
        )
        raise GoogleDeferredRetry(seconds, classification=classification)

    @api.model
    def _finish_paused(self, run, job_uuid, profile, error):
        if not self._lock_execution_fences(run, job_uuid, profile):
            return self._mark_stale(run, job_uuid)
        self.env["marketing.center.google.service"]._mark_profile_failure(
            profile, error
        )
        sync_service = self.env["marketing.center.sync.service"]
        sync_service._transition(
            run,
            "stale",
            {
                "finished_at": fields.Datetime.now(),
                "error_class": "paused",
                "error_summary": "Google Ads authorization is unavailable.",
            },
        )
        return {"stale": True, "profile_paused": True}

    @api.model
    def _finish_failure(self, run, job_uuid, *, classification, summary):
        if not self._lock_current_job(run, job_uuid):
            return {"orphan": True}
        sync_service = self.env["marketing.center.sync.service"]
        if not sync_service._validate_fencing(run):
            return {"stale": True}
        if run.state in {"planned", "queued"}:
            sync_service._transition(
                run, "running", {"started_at": fields.Datetime.now()}
            )
        terminal = "partial" if run.page_count else "failed"
        sync_service._transition(
            run,
            terminal,
            {
                "finished_at": fields.Datetime.now(),
                "error_class": self._safe_error_class(classification),
                "error_summary": summary,
            },
        )
        return {"state": terminal}

    @api.model
    def _mark_stale(self, run, job_uuid):
        if not self._lock_current_job(run, job_uuid):
            return {"orphan": True}
        sync_service = self.env["marketing.center.sync.service"]
        if not sync_service._validate_fencing(run):
            return {"stale": True}
        sync_service._transition(
            run,
            "stale",
            {
                "finished_at": fields.Datetime.now(),
                "error_summary": "Google Ads identity or profile changed before execution.",
            },
        )
        return {"stale": True}

    @api.model
    def _mark_connection_cooldown(
        self, connection, seconds, *, classification="rate_limited"
    ):
        connection = self._lock_connection_runtime(connection)
        seconds = max(1, min(int(seconds), 86_400))
        classification = (
            classification
            if classification in {"rate_limited", "transient"}
            else "transient"
        )
        cooldown_until = fields.Datetime.now() + datetime.timedelta(seconds=seconds)
        if connection.cooldown_until and connection.cooldown_until > cooldown_until:
            cooldown_until = connection.cooldown_until
        self._write_connection_runtime(
            connection,
            {
                "health_state": "degraded",
                "cooldown_until": cooldown_until,
                "last_health_error_class": classification,
                "last_health_error_message": (
                    "Google Ads quota cooldown is active."
                    if classification == "rate_limited"
                    else "Google Ads provider retry delay is active."
                ),
            },
        )

    @api.model
    def _mark_connection_success(self, connection):
        connection = self._lock_connection_runtime(connection)
        now = fields.Datetime.now()
        values = {"verified_at": now}
        if not connection.cooldown_until or connection.cooldown_until <= now:
            values.update(
                {
                    "health_state": "healthy",
                    "cooldown_until": False,
                    "last_health_error_class": False,
                    "last_health_error_message": False,
                }
            )
        self._write_connection_runtime(connection, values)

    @api.model
    def _lock_connection_runtime(self, connection):
        connection = connection.sudo().exists()
        if not connection or len(connection) != 1:
            raise ValidationError(_("A single Google Ads connection is required."))
        self.env.cr.execute(
            "SELECT id FROM marketing_center_connection WHERE id = %s FOR UPDATE",
            [connection.id],
        )
        if not self.env.cr.fetchone():
            raise ValidationError(_("The Google Ads connection no longer exists."))
        connection.invalidate_recordset(
            [
                "health_state",
                "verified_at",
                "cooldown_until",
                "last_health_error_class",
                "last_health_error_message",
            ]
        )
        return connection

    @api.model
    def _run_deferred_until(self, eta_seconds):
        eta_seconds = self._quota_delay(eta_seconds)
        if not eta_seconds:
            return False
        return fields.Datetime.now() + datetime.timedelta(seconds=eta_seconds)

    @api.model
    def _write_connection_runtime(self, connection, values):
        connection.with_context(
            marketing_google_connection_token=MARKETING_GOOGLE_CONNECTION_TOKEN,
            marketing_configuration_runtime_token=MARKETING_CONFIGURATION_RUNTIME_TOKEN,
        ).write(values)

    @api.model
    def _current_job_uuid(self, run):
        job_uuid = self.env.context.get("job_uuid")
        if not isinstance(job_uuid, str) or not job_uuid:
            return ""
        return job_uuid if run.queue_job_uuid == job_uuid else ""

    @api.model
    def _lock_current_job(self, run, job_uuid):
        self.env.cr.execute(
            "SELECT id FROM marketing_center_sync_run WHERE id = %s FOR UPDATE",
            [run.id],
        )
        if not self.env.cr.fetchone():
            return False
        run.invalidate_recordset(["queue_job_uuid", "state"])
        return bool(run.state in _ACTIVE_RUN_STATES and run.queue_job_uuid == job_uuid)

    @api.model
    def _job_attempt(self, job_uuid):
        job = self.env["queue.job"].sudo().search([("uuid", "=", job_uuid)], limit=1)
        return (job.retry if job else 0) + 1

    @api.model
    def _safe_error_class(self, value):
        value = str(value or "permanent").strip().lower()
        return value if value in _SAFE_ERROR_CLASSES else "permanent"

    @api.model
    def _source_local_date(self, source, now=None):
        instant = fields.Datetime.to_datetime(now or fields.Datetime.now())
        if not instant:
            raise ValidationError(_("The Google scheduling timestamp is invalid."))
        instant = (
            pytz.UTC.localize(instant)
            if instant.tzinfo is None
            else instant.astimezone(pytz.UTC)
        )
        try:
            zone = pytz.timezone(source.timezone)
        except pytz.UnknownTimeZoneError:
            raise ValidationError(_("The Google source timezone is invalid.")) from None
        return instant.astimezone(zone).date()

    @api.model
    def _bound(self, value, label, maximum):
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValidationError(
                _("The Google %s limit is invalid.") % label
            ) from None
        if isinstance(value, bool) or not 1 <= value <= maximum:
            raise ValidationError(_("The Google %s limit is invalid.") % label)
        return value

    @api.model
    def _unexpected_failure(self, run, job_uuid, label):
        _logger.error(
            "Unexpected Google Ads %s failure for run %s; applying bounded retry",
            label,
            run.public_ref,
        )
        if self._job_attempt(job_uuid) < _ATTEMPT_CEILING:
            raise RetryableJobError(
                "Google Ads synchronization failed unexpectedly"
            ) from None
        return self._finish_failure(
            run,
            job_uuid,
            classification="permanent",
            summary="Google Ads exhausted its unexpected-failure retry policy.",
        )

import datetime
import logging
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.google_api_base.services.errors import GoogleApiError
from odoo.addons.marketing_center_base.services.catalog_dto import (
    canonical_json,
    sha256_text,
)
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.adapter import GoogleMarketingReadAdapter
from ..services.catalog import GOOGLE_ADAPTER_KEY, GOOGLE_ADS_SERVICE
from ..services.performance import (
    GOOGLE_PERFORMANCE_GRAINS,
    google_performance_reporting_context,
    normalize_performance_window,
    performance_window_from_utc,
    validate_google_performance_cursor,
)
from .sync_common import GoogleDeferredRetry

_ACTIVE_RUN_STATES = {"planned", "queued", "running"}
_MAX_PERFORMANCE_PAGES = 4096

_logger = logging.getLogger(__name__)


class MarketingCenterSyncRun(models.Model):
    _inherit = "marketing.center.sync.run"

    def _job_sync_google_performance_page(
        self, expected_cursor_sequence, quota_attempt=0
    ):
        self.ensure_one()
        return self.env["marketing.center.google.performance.service"]._execute_page(
            self,
            expected_cursor_sequence=expected_cursor_sequence,
            quota_attempt=quota_attempt,
        )


class MarketingCenterGooglePerformanceService(models.AbstractModel):
    _name = "marketing.center.google.performance.service"
    _inherit = "marketing.center.google.sync.service"
    _description = "Marketing Center Google Ads Performance Service"

    @api.model
    def _closed_reporting_dates(self, source, *, now=None, lookback_days=7):
        lookback_days = self._bound(lookback_days, "lookback", 31)
        date_to = self._source_local_date(source, now) - datetime.timedelta(days=1)
        return date_to - datetime.timedelta(days=lookback_days - 1), date_to

    @api.model
    def _cron_enqueue_google_performance(
        self,
        source_ids=None,
        limit=25,
        now=None,
        lookback_days=7,
    ):
        result = {"queued": 0, "skipped": 0, "failed": 0}
        for source in self._scheduled_sources(
            source_ids, limit=limit, scheduler_key="performance"
        ):
            scoped = self.with_company(source.company_id).with_context(
                allowed_company_ids=[source.company_id.id]
            )
            scoped_source = scoped.env["marketing.center.source"].browse(source.id)
            try:
                connection = scoped._reader_connection(scoped_source)
                date_from, date_to = scoped._closed_reporting_dates(
                    scoped_source,
                    now=now,
                    lookback_days=lookback_days,
                )
            except Exception as error:  # pylint: disable=broad-except
                _logger.error(
                    "Google Ads performance preflight failed for source %s (%s)",
                    source.public_ref,
                    error.__class__.__name__,
                )
                result["failed"] += len(GOOGLE_PERFORMANCE_GRAINS)
                continue
            for grain in GOOGLE_PERFORMANCE_GRAINS:
                try:
                    with self.env.cr.savepoint():
                        run = scoped._plan_performance(
                            scoped_source,
                            connection,
                            grain=grain,
                            date_from=date_from,
                            date_to=date_to,
                            trigger_kind="scheduled",
                            trigger_ref="closed:%s:%s" % (date_to.isoformat(), grain),
                        )
                        if run.state != "planned":
                            result["skipped"] += 1
                            continue
                        sequence = scoped._restart_cursor(run)
                        scoped._enqueue_performance_page(run, sequence)
                        result["queued"] += 1
                except Exception as error:  # pylint: disable=broad-except
                    _logger.error(
                        "Google Ads performance scheduling failed for source %s "
                        "grain %s (%s)",
                        source.public_ref,
                        grain,
                        error.__class__.__name__,
                    )
                    result["failed"] += 1
        return result

    @api.model
    def _enqueue_manual(self, source, *, lookback_days):
        connection = self._reader_connection(source)
        date_from, date_to = self._closed_reporting_dates(
            source, lookback_days=lookback_days
        )
        queued = 0
        occurrence = str(uuid.uuid4())
        for grain in GOOGLE_PERFORMANCE_GRAINS:
            run = self._plan_performance(
                source,
                connection,
                grain=grain,
                date_from=date_from,
                date_to=date_to,
                trigger_kind="manual",
                trigger_ref="manual:%s:%s" % (occurrence, grain),
            )
            sequence = self._restart_cursor(run)
            self._enqueue_performance_page(run, sequence)
            queued += 1
        return {"queued": queued}

    @api.model
    def _plan_performance(
        self,
        source,
        connection,
        *,
        grain,
        date_from,
        date_to,
        trigger_kind,
        trigger_ref,
    ):
        self._current_profile(connection)
        date_from, date_to, window_start, window_end = normalize_performance_window(
            date_from, date_to, source.timezone
        )
        context = google_performance_reporting_context(
            source.external_account_id,
            grain,
            source.currency_id.name,
            source.timezone,
        )
        return self.env["marketing.center.sync.service"]._plan_run(
            source.company_id,
            source,
            connection,
            sync_kind="metrics",
            grain=grain,
            scope_ref=source.external_account_ref,
            trigger_kind=trigger_kind,
            trigger_ref=trigger_ref,
            reporting_context=context,
            window_start=window_start,
            window_end=window_end,
            report_timezone=source.timezone,
        )

    @api.model
    def _enqueue_performance_page(
        self,
        run,
        expected_cursor_sequence,
        *,
        eta_seconds=0,
        resume_key="",
        quota_attempt=0,
    ):
        run = self._google_performance_run(run, allow_terminal=False)
        if (
            not isinstance(expected_cursor_sequence, int)
            or isinstance(expected_cursor_sequence, bool)
            or expected_cursor_sequence < 0
        ):
            raise ValidationError(
                _("The Google performance cursor sequence is invalid.")
            )
        eta_seconds = self._quota_delay(eta_seconds)
        quota_attempt = self._quota_attempt(quota_attempt)
        identity_key = "marketing_google:performance:%s:%s" % (
            run.public_ref,
            expected_cursor_sequence,
        )
        if resume_key:
            identity_key += ":quota:%s:%s" % (quota_attempt, resume_key)
        delayed = run.with_delay(
            identity_key=identity_key,
            eta=eta_seconds or None,
            max_retries=0,
            priority=45,
            description="Sync Google Ads performance %s %s page %s"
            % (
                run.source_id.external_account_ref,
                run.grain,
                expected_cursor_sequence,
            ),
        )._job_sync_google_performance_page(expected_cursor_sequence, quota_attempt)
        sync_service = self.env["marketing.center.sync.service"]
        run_values = {
            "queue_job_uuid": str(delayed.uuid),
            "deferred_until": self._run_deferred_until(eta_seconds),
        }
        if run.state == "planned":
            sync_service._transition(run, "queued", run_values)
        else:
            sync_service._write_run(run, run_values)
        return delayed

    @api.model
    def _execute_page(self, run, *, expected_cursor_sequence, quota_attempt=0):
        quota_attempt = self._quota_attempt(quota_attempt)
        try:
            with self.env.cr.savepoint():
                return self._execute_current_page(
                    run, expected_cursor_sequence=expected_cursor_sequence
                )
        except GoogleDeferredRetry as quota_retry:
            return self._persist_quota_retry(
                run,
                quota_retry,
                expected_cursor_sequence=expected_cursor_sequence,
                quota_attempt=quota_attempt,
                enqueue_method="_enqueue_performance_page",
            )
        except RetryableJobError:
            raise
        except Exception:  # pylint: disable=broad-except
            run = run.sudo().exists()
            if not run or len(run) != 1:
                return {"missing": True}
            run.invalidate_recordset(["queue_job_uuid", "state"])
            job_uuid = self._current_job_uuid(run)
            if not job_uuid:
                return {"orphan": True}
            return self._unexpected_failure(run, job_uuid, "performance")

    @api.model
    def _execute_current_page(self, run, *, expected_cursor_sequence):
        run = self._google_performance_run(run, allow_terminal=True)
        prepared = self._prepare_performance_page(run, expected_cursor_sequence)
        if isinstance(prepared, dict):
            return prepared
        job_uuid, profile, page_token, date_from, date_to = prepared
        try:
            pages = GoogleMarketingReadAdapter(
                profile,
                expected_profile_revision=run.profile_revision,
                expected_identity_revision=run.connection_id.google_identity_revision,
                login_customer_id=run.connection_id.google_login_customer_id or "",
            ).fetch_performance_pages(
                run.source_id.external_account_id,
                run.grain,
                date_from=date_from,
                date_to=date_to,
                currency=run.source_id.currency_id.name,
                report_timezone=run.report_timezone,
                page_token=page_token,
                reporting_context_hash=run.reporting_context_hash,
                observed_at=fields.Datetime.now(),
            )
        except GoogleApiError as error:
            return self._provider_error(
                run,
                job_uuid,
                profile,
                error,
                "Google Ads performance request failed safely.",
            )
        if run.page_count + len(pages) > _MAX_PERFORMANCE_PAGES:
            return self._finish_failure(
                run,
                job_uuid,
                classification="limit_exceeded",
                summary="Google Ads performance exceeded its bounded page limit.",
            )
        return self._apply_pages(
            run,
            job_uuid,
            pages,
            expected_cursor_sequence,
            apply_method="_apply_performance_page",
            enqueue_method="_enqueue_performance_page",
        )

    @api.model
    def _prepare_performance_page(self, run, expected_cursor_sequence):
        if run.state not in _ACTIVE_RUN_STATES:
            return {"terminal": run.state}
        job_uuid = self._current_job_uuid(run)
        if not job_uuid:
            return {"orphan": True}
        if (
            not isinstance(expected_cursor_sequence, int)
            or isinstance(expected_cursor_sequence, bool)
            or expected_cursor_sequence < 0
        ):
            return self._finish_failure(
                run,
                job_uuid,
                classification="permanent",
                summary="Google Ads performance job arguments are invalid.",
            )
        if run.page_count >= _MAX_PERFORMANCE_PAGES:
            return self._finish_failure(
                run,
                job_uuid,
                classification="limit_exceeded",
                summary="Google Ads performance exceeded its bounded page limit.",
            )
        cursor_sequence, cursor_value = self._cursor_snapshot(run)
        if cursor_sequence != expected_cursor_sequence:
            return self._reschedule_current_cursor(
                run,
                job_uuid,
                enqueue_method="_enqueue_performance_page",
            )
        try:
            page_token = validate_google_performance_cursor(cursor_value)
            date_from, date_to = performance_window_from_utc(
                run.window_start, run.window_end, run.report_timezone
            )
        except GoogleApiError:
            return self._finish_failure(
                run,
                job_uuid,
                classification="permanent",
                summary="Google Ads performance cursor or window is invalid.",
            )
        profile = self._current_profile(run.connection_id, strict=False)
        if not profile or not self._preflight_current(run, profile):
            return self._mark_stale(run, job_uuid)
        expected_hash = sha256_text(
            canonical_json(
                google_performance_reporting_context(
                    run.source_id.external_account_id,
                    run.grain,
                    run.source_id.currency_id.name,
                    run.source_id.timezone,
                )
            )
        )
        if expected_hash != run.reporting_context_hash:
            return self._mark_stale(run, job_uuid)
        # Snapshot checks intentionally stay lock-free across provider I/O.
        # The page/error path takes every row lock and revalidates afterwards.
        self._cooldown_preflight(run.connection_id)
        return job_uuid, profile, page_token, date_from, date_to

    @api.model
    def _google_performance_run(self, run, *, allow_terminal):
        run = run.sudo().exists()
        if (
            not run
            or len(run) != 1
            or run.company_id not in self.env.companies
            or run.sync_kind != "metrics"
            or run.grain not in GOOGLE_PERFORMANCE_GRAINS
            or run.adapter_key != GOOGLE_ADAPTER_KEY
            or run.source_id.service != GOOGLE_ADS_SERVICE
        ):
            raise ValidationError(_("The Google performance run is invalid."))
        if not allow_terminal and run.state not in _ACTIVE_RUN_STATES:
            raise ValidationError(_("The Google performance run is terminal."))
        return run

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
from ..services.observability import (
    GOOGLE_CHANGE_ROW_LIMIT,
    GOOGLE_CHANGE_RUN_ENTITY_TYPE,
    GOOGLE_DIAGNOSTIC_RUN_ENTITY_TYPE,
    GoogleObservationPage,
    decode_google_diagnostic_cursor,
    google_change_reporting_context,
    google_diagnostic_reporting_context,
    normalize_observation_window,
    observation_date_from_utc,
)
from .sync_common import GoogleDeferredRetry

_ACTIVE_RUN_STATES = {"planned", "queued", "running"}
_MAX_CHANGE_PAGES = 128
_MAX_DIAGNOSTIC_PAGES = 1024

_logger = logging.getLogger(__name__)


class MarketingCenterSyncRun(models.Model):
    _inherit = "marketing.center.sync.run"

    def _job_sync_google_change_page(self, expected_cursor_sequence, quota_attempt=0):
        self.ensure_one()
        return self.env["marketing.center.google.change.service"]._execute_page(
            self,
            expected_cursor_sequence=expected_cursor_sequence,
            quota_attempt=quota_attempt,
        )

    def _job_sync_google_diagnostic_page(
        self, expected_cursor_sequence, quota_attempt=0
    ):
        self.ensure_one()
        return self.env["marketing.center.google.diagnostic.service"]._execute_page(
            self,
            expected_cursor_sequence=expected_cursor_sequence,
            quota_attempt=quota_attempt,
        )


class MarketingCenterSyncService(models.AbstractModel):
    _inherit = "marketing.center.sync.service"

    @api.model
    def _apply_google_change_page(self, run, page, *, expected_cursor_sequence):
        return self._apply_google_observation_page(
            run,
            page,
            expected_cursor_sequence=expected_cursor_sequence,
            entity_type=GOOGLE_CHANGE_RUN_ENTITY_TYPE,
            observation_kind="change",
            ingest_method="_ingest_change",
        )

    @api.model
    def _apply_google_diagnostic_page(self, run, page, *, expected_cursor_sequence):
        return self._apply_google_observation_page(
            run,
            page,
            expected_cursor_sequence=expected_cursor_sequence,
            entity_type=GOOGLE_DIAGNOSTIC_RUN_ENTITY_TYPE,
            observation_kind="diagnostic",
            ingest_method="_ingest_diagnostic",
        )

    @api.model
    def _apply_google_observation_page(
        self,
        run,
        page,
        *,
        expected_cursor_sequence,
        entity_type,
        observation_kind,
        ingest_method,
    ):
        run = self._validated_run(run, sync_kind="catalog")
        if run.entity_type != entity_type:
            raise ValidationError(_("The Google Ads observation run has wrong kind."))
        if (
            not isinstance(expected_cursor_sequence, int)
            or isinstance(expected_cursor_sequence, bool)
            or expected_cursor_sequence < 0
            or not isinstance(page, GoogleObservationPage)
            or page.observation_kind != observation_kind
            or page.reporting_context_hash != run.reporting_context_hash
        ):
            raise ValidationError(_("The Google Ads observation page is invalid."))
        with self.env.cr.savepoint():
            if not self._lock_active_run(run):
                return run
            if not self._validate_fencing(run):
                return run
            cursor = self._locked_cursor(run)
            if cursor.cursor_sequence != expected_cursor_sequence:
                raise ValidationError(
                    _("The Google Ads observation cursor changed concurrently.")
                )
            if run.state in {"planned", "queued"}:
                self._transition(run, "running", {"started_at": fields.Datetime.now()})
            service = self.env["marketing.center.google.observation.service"]
            results = tuple(
                getattr(service, ingest_method)(run, item) for item in page.items
            )
            self._write_cursor(
                cursor,
                {
                    "cursor_value": page.next_cursor or False,
                    "cursor_digest": (
                        sha256_text(page.next_cursor) if page.next_cursor else False
                    ),
                    "cursor_sequence": cursor.cursor_sequence + 1,
                    "provider_job_ref": False,
                    "watermark": page.watermark or False,
                    "last_success_run_id": run.id,
                    "last_advanced_at": fields.Datetime.now(),
                },
            )
            self._update_run_page(run, page, results, terminal=not page.has_more)
        return run


class GoogleObservationSyncMixin(models.AbstractModel):
    _name = "marketing.center.google.observation.sync.mixin"
    _inherit = "marketing.center.google.sync.service"
    _description = "Google Ads Observation Synchronization"

    @api.model
    def _enqueue_observation_page(
        self,
        run,
        expected_cursor_sequence,
        *,
        job_method,
        identity_prefix,
        description,
        eta_seconds=0,
        resume_key="",
        quota_attempt=0,
    ):
        run = self._google_observation_run(run, allow_terminal=False)
        if (
            not isinstance(expected_cursor_sequence, int)
            or isinstance(expected_cursor_sequence, bool)
            or expected_cursor_sequence < 0
        ):
            raise ValidationError(_("The Google observation cursor is invalid."))
        eta_seconds = self._quota_delay(eta_seconds)
        quota_attempt = self._quota_attempt(quota_attempt)
        identity_key = "%s:%s:%s" % (
            identity_prefix,
            run.public_ref,
            expected_cursor_sequence,
        )
        if resume_key:
            identity_key += ":quota:%s:%s" % (quota_attempt, resume_key)
        delayed = getattr(
            run.with_delay(
                identity_key=identity_key,
                eta=eta_seconds or None,
                max_retries=0,
                priority=47,
                description=description,
            ),
            job_method,
        )(expected_cursor_sequence, quota_attempt)
        sync_service = self.env["marketing.center.sync.service"]
        values = {
            "queue_job_uuid": str(delayed.uuid),
            "deferred_until": self._run_deferred_until(eta_seconds),
        }
        if run.state == "planned":
            sync_service._transition(run, "queued", values)
        else:
            sync_service._write_run(run, values)
        return delayed

    @api.model
    def _execute_observation_page(
        self,
        run,
        *,
        expected_cursor_sequence,
        quota_attempt,
        execute_method,
        enqueue_method,
        label,
    ):
        quota_attempt = self._quota_attempt(quota_attempt)
        try:
            with self.env.cr.savepoint():
                return getattr(self, execute_method)(
                    run, expected_cursor_sequence=expected_cursor_sequence
                )
        except GoogleDeferredRetry as deferred:
            return self._persist_quota_retry(
                run,
                deferred,
                expected_cursor_sequence=expected_cursor_sequence,
                quota_attempt=quota_attempt,
                enqueue_method=enqueue_method,
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
            return self._unexpected_failure(run, job_uuid, label)

    @api.model
    def _prepare_observation_page(
        self,
        run,
        expected_cursor_sequence,
        *,
        page_limit,
        expected_context_hash,
        enqueue_method,
    ):
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
                summary="Google Ads observation job arguments are invalid.",
            )
        if run.page_count >= page_limit:
            return self._finish_failure(
                run,
                job_uuid,
                classification="limit_exceeded",
                summary="Google Ads observation exceeded its page limit.",
            )
        cursor_sequence, cursor_value = self._cursor_snapshot(run)
        if cursor_sequence != expected_cursor_sequence:
            return self._reschedule_current_cursor(
                run,
                job_uuid,
                enqueue_method=enqueue_method,
            )
        profile = self._current_profile(run.connection_id, strict=False)
        if not profile or not self._preflight_current(run, profile):
            return self._mark_stale(run, job_uuid)
        if expected_context_hash != run.reporting_context_hash:
            return self._mark_stale(run, job_uuid)
        self._cooldown_preflight(run.connection_id)
        return job_uuid, profile, cursor_value

    @api.model
    def _google_observation_run(self, run, *, allow_terminal):
        run = run.sudo().exists()
        if (
            not run
            or len(run) != 1
            or run.company_id not in self.env.companies
            or run.sync_kind != "catalog"
            or run.entity_type
            not in {GOOGLE_CHANGE_RUN_ENTITY_TYPE, GOOGLE_DIAGNOSTIC_RUN_ENTITY_TYPE}
            or run.adapter_key != GOOGLE_ADAPTER_KEY
            or run.source_id.service != GOOGLE_ADS_SERVICE
        ):
            raise ValidationError(_("The Google Ads observation run is invalid."))
        if not allow_terminal and run.state not in _ACTIVE_RUN_STATES:
            raise ValidationError(_("The Google Ads observation run is terminal."))
        return run


class GoogleChangeService(models.AbstractModel):
    _name = "marketing.center.google.change.service"
    _inherit = "marketing.center.google.observation.sync.mixin"
    _description = "Google Ads Change History Service"

    @api.model
    def _closed_dates(self, source, *, now=None, lookback_days=3):
        lookback_days = self._bound(lookback_days, "change lookback", 30)
        date_to = self._source_local_date(source, now) - datetime.timedelta(days=1)
        date_from = date_to - datetime.timedelta(days=lookback_days - 1)
        return tuple(
            date_from + datetime.timedelta(days=offset)
            for offset in range(lookback_days)
        )

    @api.model
    def _cron_enqueue_google_changes(
        self, source_ids=None, limit=25, now=None, lookback_days=3
    ):
        result = {"queued": 0, "skipped": 0, "failed": 0}
        for source in self._scheduled_sources(
            source_ids, limit=limit, scheduler_key="changes"
        ):
            scoped = self.with_company(source.company_id).with_context(
                allowed_company_ids=[source.company_id.id]
            )
            scoped_source = scoped.env["marketing.center.source"].browse(source.id)
            try:
                with self.env.cr.savepoint():
                    connection = scoped._reader_connection(scoped_source)
                    as_of_date = scoped._source_local_date(scoped_source, now)
                    dates = scoped._closed_dates(
                        scoped_source, now=now, lookback_days=lookback_days
                    )
            except Exception as error:  # pylint: disable=broad-except
                _logger.error(
                    "Google Ads change preflight failed for source %s (%s)",
                    source.public_ref,
                    error.__class__.__name__,
                )
                result["failed"] += 1
                continue
            for local_date in dates:
                try:
                    with self.env.cr.savepoint():
                        run = scoped._plan_change(
                            scoped_source,
                            connection,
                            local_date=local_date,
                            trigger_kind="scheduled",
                            trigger_ref="changes:%s:%s"
                            % (as_of_date.isoformat(), local_date.isoformat()),
                        )
                        if run.state != "planned":
                            result["skipped"] += 1
                            continue
                        sequence = scoped._restart_cursor(run)
                        scoped._enqueue_change_page(run, sequence)
                        result["queued"] += 1
                except Exception as error:  # pylint: disable=broad-except
                    _logger.error(
                        "Google Ads change scheduling failed for source %s (%s)",
                        source.public_ref,
                        error.__class__.__name__,
                    )
                    result["failed"] += 1
        return result

    @api.model
    def _enqueue_manual(self, source, *, lookback_days=7):
        connection = self._reader_connection(source)
        queued = 0
        occurrence = str(uuid.uuid4())
        for local_date in self._closed_dates(source, lookback_days=lookback_days):
            run = self._plan_change(
                source,
                connection,
                local_date=local_date,
                trigger_kind="manual",
                trigger_ref="manual:%s:%s" % (occurrence, local_date.isoformat()),
            )
            sequence = self._restart_cursor(run)
            self._enqueue_change_page(run, sequence)
            queued += 1
        return {"queued": queued}

    @api.model
    def _plan_change(
        self,
        source,
        connection,
        *,
        local_date,
        trigger_kind,
        trigger_ref,
    ):
        start, end = normalize_observation_window(local_date, source.timezone)
        context = google_change_reporting_context(
            source.external_account_id, local_date, source.timezone
        )
        return self.env["marketing.center.sync.service"]._plan_run(
            source.company_id,
            source,
            connection,
            sync_kind="catalog",
            entity_type=GOOGLE_CHANGE_RUN_ENTITY_TYPE,
            scope_ref=source.external_account_ref,
            trigger_kind=trigger_kind,
            trigger_ref=trigger_ref,
            reporting_context=context,
            window_start=start,
            window_end=end,
            report_timezone=source.timezone,
        )

    @api.model
    def _enqueue_change_page(
        self,
        run,
        expected_cursor_sequence,
        *,
        eta_seconds=0,
        resume_key="",
        quota_attempt=0,
    ):
        return self._enqueue_observation_page(
            run,
            expected_cursor_sequence,
            job_method="_job_sync_google_change_page",
            identity_prefix="marketing_google:changes",
            description="Sync Google Ads change history %s"
            % run.source_id.external_account_ref,
            eta_seconds=eta_seconds,
            resume_key=resume_key,
            quota_attempt=quota_attempt,
        )

    @api.model
    def _execute_page(self, run, *, expected_cursor_sequence, quota_attempt=0):
        return self._execute_observation_page(
            run,
            expected_cursor_sequence=expected_cursor_sequence,
            quota_attempt=quota_attempt,
            execute_method="_execute_current_page",
            enqueue_method="_enqueue_change_page",
            label="change history",
        )

    @api.model
    def _execute_current_page(self, run, *, expected_cursor_sequence):
        run = self._google_observation_run(run, allow_terminal=True)
        if run.entity_type != GOOGLE_CHANGE_RUN_ENTITY_TYPE:
            raise ValidationError(_("The Google Ads change run is invalid."))
        local_date = observation_date_from_utc(
            run.window_start, run.window_end, run.report_timezone
        )
        context_hash = sha256_text(
            canonical_json(
                google_change_reporting_context(
                    run.source_id.external_account_id,
                    local_date,
                    run.report_timezone,
                )
            )
        )
        prepared = self._prepare_observation_page(
            run,
            expected_cursor_sequence,
            page_limit=_MAX_CHANGE_PAGES,
            expected_context_hash=context_hash,
            enqueue_method="_enqueue_change_page",
        )
        if isinstance(prepared, dict):
            return prepared
        job_uuid, profile, page_token = prepared
        try:
            pages = GoogleMarketingReadAdapter(
                profile,
                expected_profile_revision=run.profile_revision,
                expected_identity_revision=run.connection_id.google_identity_revision,
                login_customer_id=run.connection_id.google_login_customer_id or "",
            ).fetch_change_pages(
                run.source_id.external_account_id,
                local_date=local_date,
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
                "Google Ads change-history request failed safely.",
            )
        received = sum(len(page.items) for page in pages)
        if (
            run.page_count + len(pages) > _MAX_CHANGE_PAGES
            or run.received_count + received > GOOGLE_CHANGE_ROW_LIMIT
        ):
            return self._finish_failure(
                run,
                job_uuid,
                classification="limit_exceeded",
                summary="Google Ads change history reached its safe 10,000-row cap.",
            )
        return self._apply_pages(
            run,
            job_uuid,
            pages,
            expected_cursor_sequence,
            apply_method="_apply_google_change_page",
            enqueue_method="_enqueue_change_page",
        )


class GoogleDiagnosticService(models.AbstractModel):
    _name = "marketing.center.google.diagnostic.service"
    _inherit = "marketing.center.google.observation.sync.mixin"
    _description = "Google Ads Delivery Diagnostic Service"

    @api.model
    def _cron_enqueue_google_diagnostics(self, source_ids=None, limit=25, now=None):
        result = {"queued": 0, "skipped": 0, "failed": 0}
        for source in self._scheduled_sources(
            source_ids, limit=limit, scheduler_key="diagnostics"
        ):
            scoped = self.with_company(source.company_id).with_context(
                allowed_company_ids=[source.company_id.id]
            )
            scoped_source = scoped.env["marketing.center.source"].browse(source.id)
            try:
                with self.env.cr.savepoint():
                    connection = scoped._reader_connection(scoped_source)
                    local_date = scoped._source_local_date(scoped_source, now)
                    run = scoped._plan_diagnostics(
                        scoped_source,
                        connection,
                        trigger_kind="scheduled",
                        trigger_ref="diagnostics:%s" % local_date.isoformat(),
                    )
                    if run.state != "planned":
                        result["skipped"] += 1
                        continue
                    sequence = scoped._restart_cursor(run)
                    scoped._enqueue_diagnostic_page(run, sequence)
                    result["queued"] += 1
            except Exception as error:  # pylint: disable=broad-except
                _logger.error(
                    "Google Ads diagnostic scheduling failed for source %s (%s)",
                    source.public_ref,
                    error.__class__.__name__,
                )
                result["failed"] += 1
        return result

    @api.model
    def _enqueue_manual(self, source):
        connection = self._reader_connection(source)
        run = self._plan_diagnostics(
            source,
            connection,
            trigger_kind="manual",
            trigger_ref="manual:%s" % uuid.uuid4(),
        )
        sequence = self._restart_cursor(run)
        self._enqueue_diagnostic_page(run, sequence)
        return {"queued": 1}

    @api.model
    def _plan_diagnostics(self, source, connection, *, trigger_kind, trigger_ref):
        context = google_diagnostic_reporting_context(source.external_account_id)
        return self.env["marketing.center.sync.service"]._plan_run(
            source.company_id,
            source,
            connection,
            sync_kind="catalog",
            entity_type=GOOGLE_DIAGNOSTIC_RUN_ENTITY_TYPE,
            scope_ref=source.external_account_ref,
            trigger_kind=trigger_kind,
            trigger_ref=trigger_ref,
            reporting_context=context,
            report_timezone=source.timezone,
        )

    @api.model
    def _enqueue_diagnostic_page(
        self,
        run,
        expected_cursor_sequence,
        *,
        eta_seconds=0,
        resume_key="",
        quota_attempt=0,
    ):
        return self._enqueue_observation_page(
            run,
            expected_cursor_sequence,
            job_method="_job_sync_google_diagnostic_page",
            identity_prefix="marketing_google:diagnostics",
            description="Sync Google Ads delivery diagnostics %s"
            % run.source_id.external_account_ref,
            eta_seconds=eta_seconds,
            resume_key=resume_key,
            quota_attempt=quota_attempt,
        )

    @api.model
    def _execute_page(self, run, *, expected_cursor_sequence, quota_attempt=0):
        return self._execute_observation_page(
            run,
            expected_cursor_sequence=expected_cursor_sequence,
            quota_attempt=quota_attempt,
            execute_method="_execute_current_page",
            enqueue_method="_enqueue_diagnostic_page",
            label="delivery diagnostic",
        )

    @api.model
    def _execute_current_page(self, run, *, expected_cursor_sequence):
        run = self._google_observation_run(run, allow_terminal=True)
        if run.entity_type != GOOGLE_DIAGNOSTIC_RUN_ENTITY_TYPE:
            raise ValidationError(_("The Google Ads diagnostic run is invalid."))
        context_hash = sha256_text(
            canonical_json(
                google_diagnostic_reporting_context(run.source_id.external_account_id)
            )
        )
        prepared = self._prepare_observation_page(
            run,
            expected_cursor_sequence,
            page_limit=_MAX_DIAGNOSTIC_PAGES,
            expected_context_hash=context_hash,
            enqueue_method="_enqueue_diagnostic_page",
        )
        if isinstance(prepared, dict):
            return prepared
        job_uuid, profile, cursor_value = prepared
        try:
            stage, page_token = decode_google_diagnostic_cursor(cursor_value)
            pages = GoogleMarketingReadAdapter(
                profile,
                expected_profile_revision=run.profile_revision,
                expected_identity_revision=run.connection_id.google_identity_revision,
                login_customer_id=run.connection_id.google_login_customer_id or "",
            ).fetch_diagnostic_pages(
                run.source_id.external_account_id,
                stage,
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
                "Google Ads diagnostic request failed safely.",
            )
        if run.page_count + len(pages) > _MAX_DIAGNOSTIC_PAGES:
            return self._finish_failure(
                run,
                job_uuid,
                classification="limit_exceeded",
                summary="Google Ads diagnostics exceeded its bounded page limit.",
            )
        return self._apply_pages(
            run,
            job_uuid,
            pages,
            expected_cursor_sequence,
            apply_method="_apply_google_diagnostic_page",
            enqueue_method="_enqueue_diagnostic_page",
        )

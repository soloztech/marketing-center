import datetime
import logging
import uuid

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.marketing_center_base.services.catalog_dto import (
    canonical_json,
    sha256_text,
)
from odoo.addons.meta_api_base.services.errors import (
    MetaApiError,
    MetaApiPausedError,
    MetaApiRateLimitError,
    MetaApiTransientError,
)
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.adapter import (
    META_ADAPTER_KEY,
    META_ADS_SERVICE,
    MetaMarketingReadAdapter,
)
from ..services.insights import (
    META_INSIGHTS_GRAINS,
    insights_window_from_utc,
    meta_insights_reporting_context,
    normalize_insights_window,
)

_ACTIVE_RUN_STATES = {"planned", "queued", "running"}
_INSIGHTS_ATTEMPT_CEILING = 8
_MAX_INSIGHTS_PAGES = 512

_logger = logging.getLogger(__name__)


class MarketingCenterSyncRun(models.Model):
    _inherit = "marketing.center.sync.run"

    def _job_sync_meta_insights_page(self, expected_cursor_sequence):
        self.ensure_one()
        return self.env["marketing.center.meta.catalog.service"]._execute_insights_page(
            self,
            expected_cursor_sequence=expected_cursor_sequence,
        )


class MarketingCenterMetaInsightsService(models.AbstractModel):
    """Meta Insights orchestration over the shared read/fencing facade.

    The catalog service inheritance reuses only Meta profile, queue-job and
    connection fencing primitives.  Metric projection remains provider-neutral
    in ``marketing.center.sync.service``.
    """

    _inherit = "marketing.center.meta.catalog.service"

    @api.model
    def _insights_reader_connection(self, source):
        connection = self._reader_connection(source)
        capabilities = connection.effective_capabilities_json or {}
        if capabilities.get("read_metrics") is not True:
            raise ValidationError(
                _("The Meta reader connection cannot read performance metrics.")
            )
        return connection

    @api.model
    def _plan_insights(
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
        source = source.sudo().exists()
        connection = connection.sudo().exists()
        if (
            not source
            or len(source) != 1
            or source.service != META_ADS_SERVICE
            or not connection
            or len(connection) != 1
            or connection.source_id != source
        ):
            raise ValidationError(_("A valid Meta Ads reader is required."))
        if self._insights_reader_connection(source) != connection:
            raise ValidationError(
                _("The selected Meta connection cannot read performance metrics.")
            )
        profile = self._current_profile(connection)
        try:
            (
                _date_from,
                _date_to,
                window_start,
                window_end,
            ) = normalize_insights_window(date_from, date_to, source.timezone)
            reporting_context = meta_insights_reporting_context(
                profile.graph_version,
                source.external_account_ref,
                grain,
                source.currency_id.name,
                source.timezone,
            )
        except MetaApiError as error:
            raise ValidationError(_("The Meta Insights request is invalid.")) from error
        return self.env["marketing.center.sync.service"]._plan_run(
            source.company_id,
            source,
            connection,
            sync_kind="metrics",
            grain=grain,
            scope_ref=source.external_account_ref,
            trigger_kind=trigger_kind,
            trigger_ref=trigger_ref,
            reporting_context=reporting_context,
            window_start=window_start,
            window_end=window_end,
            report_timezone=source.timezone,
        )

    @api.model
    def _enqueue_insights_page(self, run, expected_cursor_sequence):
        run = self._meta_insights_run(run, allow_terminal=False)
        if (
            not isinstance(expected_cursor_sequence, int)
            or isinstance(expected_cursor_sequence, bool)
            or expected_cursor_sequence < 0
        ):
            raise ValidationError(_("The Meta Insights cursor sequence is invalid."))
        delayed = run.with_delay(
            identity_key="marketing_meta:insights:%s:%s"
            % (run.public_ref, expected_cursor_sequence),
            max_retries=0,
            priority=45,
            description="Sync Meta Insights %s %s page %s"
            % (
                run.source_id.external_account_ref,
                run.grain,
                expected_cursor_sequence,
            ),
        )._job_sync_meta_insights_page(expected_cursor_sequence)
        sync_service = self.env["marketing.center.sync.service"]
        if run.state == "planned":
            sync_service._transition(
                run,
                "queued",
                {"queue_job_uuid": str(delayed.uuid)},
            )
        else:
            sync_service._write_run(run, {"queue_job_uuid": str(delayed.uuid)})
        return delayed

    @api.model
    def _restart_insights_cursor(self, run):
        """Clear transport progress for an explicit full replay.

        Facts remain revisioned and idempotent in the provider-neutral core; only
        the opaque Meta pagination position is reset.  This is deliberately not
        automatic for every permanent provider error because not every error is
        evidence that the cursor is invalid.
        """

        run = self._meta_insights_run(run, allow_terminal=False)
        if run.state != "planned":
            raise ValidationError(
                _("Meta Insights can restart only before the run is queued.")
            )
        sync_service = self.env["marketing.center.sync.service"]
        if not sync_service._validate_fencing(run):
            raise ValidationError(
                _("Meta Insights configuration changed before restart.")
            )
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
    def _execute_insights_page(self, run, *, expected_cursor_sequence):
        try:
            with self.env.cr.savepoint():
                return self._execute_current_insights_page(
                    run,
                    expected_cursor_sequence=expected_cursor_sequence,
                )
        except RetryableJobError:
            raise
        except Exception:  # pylint: disable=broad-except
            run = run.sudo().exists()
            if not run or len(run) != 1:
                return {"missing": True}
            run.invalidate_recordset(
                ["queue_job_uuid", "state", "page_count", "error_class"]
            )
            job_uuid = self._current_job_uuid(run)
            if not job_uuid:
                return {"orphan": True}
            _logger.error(
                "Unexpected Meta Insights job failure for run %s; "
                "applying bounded retry",
                run.public_ref,
            )
            if self._job_attempt(job_uuid) < _INSIGHTS_ATTEMPT_CEILING:
                raise RetryableJobError(
                    "Meta Insights job failed unexpectedly"
                ) from None
            return self._finish_insights_failure(
                run,
                job_uuid,
                classification="permanent",
                summary="Meta Insights exhausted its unexpected-failure retry policy.",
            )

    @api.model
    def _execute_current_insights_page(self, run, *, expected_cursor_sequence):
        run = self._meta_insights_run(run, allow_terminal=True)
        prepared = self._prepare_insights_execution(run, expected_cursor_sequence)
        if isinstance(prepared, dict):
            return prepared
        job_uuid, after, profile, date_from, date_to = prepared
        fetched = self._fetch_insights_provider_page(
            run,
            job_uuid,
            profile,
            after,
            date_from,
            date_to,
        )
        if isinstance(fetched, dict):
            return fetched
        return self._apply_insights_provider_page(
            run,
            job_uuid,
            fetched,
            expected_cursor_sequence,
        )

    @api.model
    def _prepare_insights_execution(self, run, expected_cursor_sequence):
        if not run or run.state not in _ACTIVE_RUN_STATES:
            return {"terminal": run.state if run else "missing"}
        job_uuid = self._current_job_uuid(run)
        if not job_uuid:
            return {"orphan": True}
        if (
            not isinstance(expected_cursor_sequence, int)
            or isinstance(expected_cursor_sequence, bool)
            or expected_cursor_sequence < 0
        ):
            return self._finish_insights_failure(
                run,
                job_uuid,
                classification="permanent",
                summary="Meta Insights job arguments are invalid.",
            )
        if run.page_count >= _MAX_INSIGHTS_PAGES:
            return self._finish_insights_failure(
                run,
                job_uuid,
                classification="permanent",
                summary="Meta Insights exceeded its bounded page limit.",
            )
        cursor_sequence, after = self._insights_cursor_snapshot(run)
        if cursor_sequence != expected_cursor_sequence:
            return {"cursor_changed": True}
        try:
            after = self._validated_insights_cursor(after)
        except MetaApiError as error:
            return self._finish_insights_failure(
                run,
                job_uuid,
                classification=getattr(error, "classification", "permanent"),
                summary=(
                    "Meta Insights cursor is invalid; retry this window from "
                    "the first page."
                ),
            )
        try:
            date_from, date_to = insights_window_from_utc(
                run.window_start,
                run.window_end,
                run.report_timezone,
            )
        except MetaApiError as error:
            return self._finish_insights_failure(
                run,
                job_uuid,
                classification=getattr(error, "classification", "permanent"),
                summary="Meta Insights cursor or window is invalid.",
            )
        profile = self._current_profile(run.connection_id, strict=False)
        if not profile or not self._preflight_current(run, profile):
            return self._mark_insights_stale_before_io(run, job_uuid)
        try:
            expected_context_hash = sha256_text(
                canonical_json(
                    meta_insights_reporting_context(
                        profile.graph_version,
                        run.source_id.external_account_ref,
                        run.grain,
                        run.source_id.currency_id.name,
                        run.source_id.timezone,
                    )
                )
            )
        except MetaApiError:
            return self._mark_insights_stale_before_io(run, job_uuid)
        if expected_context_hash != run.reporting_context_hash:
            return self._mark_insights_stale_before_io(run, job_uuid)
        return job_uuid, after, profile, date_from, date_to

    @api.model
    def _fetch_insights_provider_page(
        self,
        run,
        job_uuid,
        profile,
        after,
        date_from,
        date_to,
    ):
        try:
            return MetaMarketingReadAdapter(profile).fetch_insights_page(
                run.source_id.external_account_ref,
                run.grain,
                date_from=date_from,
                date_to=date_to,
                currency=run.source_id.currency_id.name,
                report_timezone=run.report_timezone,
                after=after,
                reporting_context_hash=run.reporting_context_hash,
            )
        except MetaApiRateLimitError as error:
            return self._retry_or_finish_insights(run, job_uuid, error)
        except MetaApiTransientError as error:
            return self._retry_or_finish_insights(run, job_uuid, error)
        except MetaApiPausedError as error:
            return self._finish_insights_paused(run, job_uuid, profile, error)
        except MetaApiError as error:
            return self._finish_insights_failure(
                run,
                job_uuid,
                classification=getattr(error, "classification", "permanent"),
                summary="Meta Insights request failed safely.",
            )

    @api.model
    def _apply_insights_provider_page(
        self,
        run,
        job_uuid,
        page,
        expected_cursor_sequence,
    ):
        if not self._lock_current_job(run, job_uuid):
            return {"orphan": True}
        sync_service = self.env["marketing.center.sync.service"]
        try:
            with self.env.cr.savepoint():
                sync_service._apply_performance_page(
                    run,
                    page,
                    expected_cursor_sequence=expected_cursor_sequence,
                )
                run.invalidate_recordset(["state", "page_count"])
                if page.has_more and run.state == "running":
                    self._enqueue_insights_page(
                        run,
                        expected_cursor_sequence + 1,
                    )
        except ValidationError:
            run.invalidate_recordset(["state", "page_count", "queue_job_uuid"])
            return self._finish_insights_failure(
                run,
                job_uuid,
                classification="permanent",
                summary="Meta Insights page contract could not be applied.",
            )
        run.invalidate_recordset(["state", "page_count"])
        if run.state == "stale":
            return {"stale": True}
        return {
            "state": run.state,
            "grain": run.grain,
            "received": len(page.items),
            "has_more": page.has_more,
        }

    @api.model
    def _retry_or_finish_insights(self, run, job_uuid, error):
        if self._job_attempt(job_uuid) < _INSIGHTS_ATTEMPT_CEILING:
            raise RetryableJobError(
                "Meta Insights is temporarily unavailable",
                seconds=getattr(error, "retry_after_seconds", 0) or None,
            ) from None
        return self._finish_insights_failure(
            run,
            job_uuid,
            classification=getattr(error, "classification", "transient"),
            summary="Meta Insights exhausted its bounded retry policy.",
        )

    @api.model
    def _finish_insights_failure(
        self,
        run,
        job_uuid,
        *,
        classification,
        summary,
    ):
        if not self._lock_current_job(run, job_uuid):
            return {"orphan": True}
        sync_service = self.env["marketing.center.sync.service"]
        if not sync_service._validate_fencing(run):
            return {"stale": True}
        if run.state in {"planned", "queued"}:
            sync_service._transition(
                run,
                "running",
                {"started_at": fields.Datetime.now()},
            )
        terminal_state = "partial" if run.page_count else "failed"
        sync_service._transition(
            run,
            terminal_state,
            {
                "finished_at": fields.Datetime.now(),
                "error_class": self._safe_error_class(classification),
                "error_summary": summary,
            },
        )
        return {"state": terminal_state}

    @api.model
    def _finish_insights_paused(self, run, job_uuid, profile, error):
        if not self._lock_current_job(run, job_uuid):
            return {"orphan": True}
        meta_service = self.env["marketing.center.meta.service"]
        if not meta_service._lock_current_profile(profile, run.profile_revision):
            return self._mark_insights_stale_before_io(run, job_uuid)
        sync_service = self.env["marketing.center.sync.service"]
        if not sync_service._validate_fencing(run):
            return {"stale": True}
        meta_service._mark_profile_failure(profile, error, paused=True)
        sync_service._transition(
            run,
            "stale",
            {
                "finished_at": fields.Datetime.now(),
                "error_class": "paused",
                "error_summary": "Meta Insights authorization is unavailable.",
            },
        )
        return {"stale": True, "profile_paused": True}

    @api.model
    def _mark_insights_stale_before_io(self, run, job_uuid):
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
                "error_summary": "Meta Insights profile changed before execution.",
            },
        )
        return {"stale": True}

    @api.model
    def _insights_cursor_snapshot(self, run):
        cursor = (
            self.env["marketing.center.sync.cursor"]
            .sudo()
            .search(
                [
                    ("source_id", "=", run.source_id.id),
                    ("adapter_key", "=", run.adapter_key),
                    ("cursor_kind", "=", "metrics"),
                    ("entity_type", "=", False),
                    ("grain", "=", run.grain),
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
    def _validated_insights_cursor(self, value):
        if value in (None, ""):
            return ""
        if not isinstance(value, str) or len(value.encode("utf-8")) > 3072:
            raise MetaApiError("Meta Insights cursor is invalid")
        if any(ord(character) < 32 for character in value):
            raise MetaApiError("Meta Insights cursor is invalid")
        return value

    @api.model
    def _meta_insights_run(self, run, *, allow_terminal):
        run = run.sudo().exists()
        if (
            not run
            or run._name != "marketing.center.sync.run"
            or len(run) != 1
            or run.company_id not in self.env.companies
            or run.sync_kind != "metrics"
            or run.grain not in META_INSIGHTS_GRAINS
            or run.entity_type
            or run.adapter_key != META_ADAPTER_KEY
            or run.source_id.service != META_ADS_SERVICE
            or run.scope_ref != run.source_id.external_account_ref
        ):
            raise ValidationError(_("A valid Meta Insights run is required."))
        if not allow_terminal and run.state not in _ACTIVE_RUN_STATES:
            raise ValidationError(_("The Meta Insights run is already terminal."))
        return run


class MarketingCenterSource(models.Model):
    _inherit = "marketing.center.source"

    def action_enqueue_meta_insights_sync(self):
        self.ensure_one()
        self.check_access_rights("write")
        self.check_access_rule("write")
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_admin"
        ):
            raise AccessError(_("Only Marketing Center administrators can sync Meta."))
        service = self.env["marketing.center.meta.catalog.service"]
        connection = service._insights_reader_connection(self)
        try:
            zone = pytz.timezone(self.timezone)
        except pytz.UnknownTimeZoneError as error:
            raise ValidationError(_("The Meta source timezone is invalid.")) from error
        today = pytz.UTC.localize(datetime.datetime.utcnow()).astimezone(zone).date()
        date_to = today - datetime.timedelta(days=1)
        date_from = date_to - datetime.timedelta(days=6)
        occurrence = str(uuid.uuid4())
        for grain in META_INSIGHTS_GRAINS:
            run = service._plan_insights(
                self,
                connection,
                grain=grain,
                date_from=date_from,
                date_to=date_to,
                trigger_kind="manual",
                trigger_ref="manual:%s:%s" % (occurrence, grain),
            )
            cursor_sequence = service._restart_insights_cursor(run)
            service._enqueue_insights_page(
                run,
                cursor_sequence,
            )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Meta Insights"),
                "message": _(
                    "The last seven closed days were queued for account and "
                    "campaign performance."
                ),
                "type": "info",
                "sticky": False,
            },
        }

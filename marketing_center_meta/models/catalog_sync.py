import logging

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

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
from ..services.catalog import (
    META_CATALOG_RUN_ENTITY_TYPE,
    decode_meta_catalog_cursor,
    meta_catalog_sweep_reporting_context,
    orchestrate_meta_catalog_page,
)

_ACTIVE_RUN_STATES = {"planned", "queued", "running"}
_CATALOG_ATTEMPT_CEILING = 8
_MAX_CATALOG_PAGES = 512

_logger = logging.getLogger(__name__)


class MarketingCenterSyncRun(models.Model):
    _inherit = "marketing.center.sync.run"

    def _job_sync_meta_catalog_page(self, expected_cursor_sequence):
        self.ensure_one()
        return self.env["marketing.center.meta.catalog.service"]._execute_page(
            self,
            expected_cursor_sequence=expected_cursor_sequence,
        )


class MarketingCenterMetaCatalogService(models.AbstractModel):
    _name = "marketing.center.meta.catalog.service"
    _description = "Marketing Center Meta Catalog Service"

    @api.model
    def _scheduled_sources(self, source_ids=None, *, limit=50, scheduler_key):
        try:
            limit = int(limit)
        except (TypeError, ValueError) as error:
            raise ValidationError(
                _("The scheduled source limit is invalid.")
            ) from error
        if isinstance(limit, bool) or limit < 1 or limit > 500:
            raise ValidationError(_("The scheduled source limit is invalid."))
        domain = [
            ("active", "=", True),
            ("service", "=", META_ADS_SERVICE),
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
            cursor_key="meta.%s" % scheduler_key,
            limit=limit,
        )

    @api.model
    def _source_local_date(self, source, now=None):
        instant = fields.Datetime.to_datetime(now or fields.Datetime.now())
        if not instant:
            raise ValidationError(_("The scheduling timestamp is invalid."))
        if instant.tzinfo is None:
            instant = pytz.UTC.localize(instant)
        else:
            instant = instant.astimezone(pytz.UTC)
        try:
            zone = pytz.timezone(source.timezone)
        except pytz.UnknownTimeZoneError as error:
            raise ValidationError(_("The Meta source timezone is invalid.")) from error
        return instant.astimezone(zone).date()

    @api.model
    def _cron_enqueue_meta_catalog(self, source_ids=None, limit=50, now=None):
        """Queue at most one complete catalog sweep per source and local day."""

        result = {"queued": 0, "skipped": 0, "failed": 0}
        for source in self._scheduled_sources(
            source_ids, limit=limit, scheduler_key="catalog"
        ):
            scoped_service = self.with_company(source.company_id).with_context(
                allowed_company_ids=[source.company_id.id]
            )
            scoped_source = scoped_service.env["marketing.center.source"].browse(
                source.id
            )
            try:
                with self.env.cr.savepoint():
                    connection = scoped_service._reader_connection(scoped_source)
                    local_date = scoped_service._source_local_date(scoped_source, now)
                    run = scoped_service._plan_sweep(
                        scoped_source,
                        connection,
                        trigger_kind="scheduled",
                        trigger_ref="catalog:%s" % local_date.isoformat(),
                    )
                    if run.state != "planned":
                        result["skipped"] += 1
                        continue
                    cursor_sequence = scoped_service._restart_catalog_cursor(run)
                    scoped_service._enqueue_page(run, cursor_sequence)
                    result["queued"] += 1
            except Exception as error:  # pylint: disable=broad-except
                # One invalid/rotating account cannot suppress all other tenants.
                # Do not log exception text: provider errors may embed response data.
                _logger.error(
                    "Meta catalog scheduling failed for source %s (%s)",
                    source.public_ref,
                    error.__class__.__name__,
                )
                result["failed"] += 1
        return result

    @api.model
    def _reader_connection(self, source):
        source = source.sudo().exists()
        if (
            not source
            or len(source) != 1
            or source.company_id not in self.env.companies
            or source.service != META_ADS_SERVICE
        ):
            raise ValidationError(_("A valid Meta Ads source is required."))
        connection = (
            self.env["marketing.center.connection"]
            .sudo()
            .search(
                [
                    ("source_id", "=", source.id),
                    ("adapter_key", "=", META_ADAPTER_KEY),
                    ("purpose", "=", "reader"),
                    ("active", "=", True),
                    ("state", "=", "ready"),
                ],
                limit=1,
            )
        )
        if not connection:
            raise ValidationError(_("The Meta reader connection is not ready."))
        return connection

    @api.model
    def _plan_sweep(
        self,
        source,
        connection,
        *,
        trigger_kind,
        trigger_ref,
    ):
        profile = self._current_profile(connection)
        reporting_context = meta_catalog_sweep_reporting_context(
            profile.meta_app_id.graph_version,
            source.external_account_ref,
        )
        return self.env["marketing.center.sync.service"]._plan_run(
            source.company_id,
            source,
            connection,
            sync_kind="catalog",
            entity_type=META_CATALOG_RUN_ENTITY_TYPE,
            scope_ref=source.external_account_ref,
            trigger_kind=trigger_kind,
            trigger_ref=trigger_ref,
            reporting_context=reporting_context,
            report_timezone=source.timezone,
        )

    @api.model
    def _enqueue_page(self, run, expected_cursor_sequence):
        run = self._meta_run(run, allow_terminal=False)
        if (
            not isinstance(expected_cursor_sequence, int)
            or isinstance(expected_cursor_sequence, bool)
            or expected_cursor_sequence < 0
        ):
            raise ValidationError(_("The Meta catalog cursor sequence is invalid."))
        delayed = run.with_delay(
            identity_key="marketing_meta:catalog:%s:%s"
            % (run.public_ref, expected_cursor_sequence),
            max_retries=0,
            priority=40,
            description="Sync Meta catalog %s page %s"
            % (run.source_id.external_account_ref, expected_cursor_sequence),
        )._job_sync_meta_catalog_page(expected_cursor_sequence)
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
    def _restart_catalog_cursor(self, run):
        """Start one authoritative catalog sweep at the hierarchy root.

        The core cursor intentionally survives runs, which is useful for resumable
        pagination inside one sweep.  A *new* full sweep, however, must not inherit
        an opaque provider cursor left by a failed predecessor.  Reset only the
        transport position; catalog entities/revisions remain idempotent evidence.
        """

        run = self._meta_run(run, allow_terminal=False)
        if run.state != "planned":
            raise ValidationError(
                _("A Meta catalog sweep can restart only before it is queued.")
            )
        sync_service = self.env["marketing.center.sync.service"]
        if not sync_service._validate_fencing(run):
            raise ValidationError(
                _("Meta catalog configuration changed before restart.")
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
    def _execute_page(self, run, *, expected_cursor_sequence):
        try:
            # Any unexpected exception rolls back the complete attempt before the
            # bounded retry/terminal policy is evaluated.
            with self.env.cr.savepoint():
                return self._execute_current_page(
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
            # Deliberately omit the exception and traceback: provider/ORM errors can
            # contain response fragments or credential-bearing request details.
            _logger.error(
                "Unexpected Meta catalog job failure for run %s; applying bounded retry",
                run.public_ref,
            )
            if self._job_attempt(job_uuid) < _CATALOG_ATTEMPT_CEILING:
                raise RetryableJobError(
                    "Meta catalog job failed unexpectedly"
                ) from None
            return self._finish_failure(
                run,
                job_uuid,
                classification="permanent",
                summary="Meta catalog exhausted its unexpected-failure retry policy.",
            )

    @api.model
    def _execute_current_page(self, run, *, expected_cursor_sequence):
        run = self._meta_run(run, allow_terminal=True)
        prepared = self._prepare_page_execution(run, expected_cursor_sequence)
        if isinstance(prepared, dict):
            return prepared
        job_uuid, stage, after, profile = prepared
        fetched = self._fetch_provider_page(
            run,
            job_uuid,
            profile,
            stage,
            after,
        )
        if isinstance(fetched, dict):
            return fetched
        return self._apply_provider_page(
            run,
            job_uuid,
            stage,
            fetched,
            expected_cursor_sequence,
        )

    @api.model
    def _prepare_page_execution(self, run, expected_cursor_sequence):
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
            return self._finish_failure(
                run,
                job_uuid,
                classification="permanent",
                summary="Meta catalog job arguments are invalid.",
            )
        if run.page_count >= _MAX_CATALOG_PAGES:
            return self._finish_failure(
                run,
                job_uuid,
                classification="permanent",
                summary="Meta catalog exceeded its bounded page limit.",
            )
        cursor_sequence, cursor_value = self._cursor_snapshot(run)
        if cursor_sequence != expected_cursor_sequence:
            return self._reschedule_current_cursor(
                run,
                job_uuid,
                enqueue_method="_enqueue_page",
            )
        try:
            stage, after = decode_meta_catalog_cursor(cursor_value)
        except MetaApiError as error:
            return self._finish_failure(
                run,
                job_uuid,
                classification=getattr(error, "classification", "permanent"),
                summary="Meta catalog cursor is invalid.",
            )
        profile = self._current_profile(run.connection_id, strict=False)
        if not profile or not self._preflight_current(run, profile):
            return self._mark_stale_before_io(run, job_uuid)
        expected_context_hash = sha256_text(
            canonical_json(
                meta_catalog_sweep_reporting_context(
                    profile.meta_app_id.graph_version,
                    run.source_id.external_account_ref,
                )
            )
        )
        if expected_context_hash != run.reporting_context_hash:
            return self._mark_stale_before_io(run, job_uuid)
        return job_uuid, stage, after, profile

    @api.model
    def _fetch_provider_page(self, run, job_uuid, profile, stage, after):
        try:
            provider_page = MetaMarketingReadAdapter(
                profile,
                expected_app_revision=profile.meta_app_id.revision,
            ).fetch_catalog_page(
                run.source_id.external_account_ref,
                stage,
                after=after,
                reporting_context_hash=run.reporting_context_hash,
            )
            page = orchestrate_meta_catalog_page(stage, provider_page)
        except MetaApiRateLimitError as error:
            return self._retry_or_finish(run, job_uuid, error)
        except MetaApiTransientError as error:
            return self._retry_or_finish(run, job_uuid, error)
        except MetaApiPausedError as error:
            return self._finish_paused(run, job_uuid, profile, error)
        except MetaApiError as error:
            return self._finish_failure(
                run,
                job_uuid,
                classification=getattr(error, "classification", "permanent"),
                summary="Meta catalog request failed safely.",
            )
        return page

    @api.model
    def _apply_provider_page(
        self,
        run,
        job_uuid,
        stage,
        page,
        expected_cursor_sequence,
    ):
        if not self._lock_current_job(run, job_uuid):
            return {"orphan": True}
        sync_service = self.env["marketing.center.sync.service"]
        try:
            # Page projection and successor enqueue are atomic.  A contract/CAS
            # failure rolls the complete page back before the run is closed.
            with self.env.cr.savepoint():
                sync_service._apply_entity_page(
                    run,
                    page,
                    expected_cursor_sequence=expected_cursor_sequence,
                )
                run.invalidate_recordset(["state", "page_count"])
                if page.has_more and run.state == "running":
                    self._enqueue_page(run, expected_cursor_sequence + 1)
        except ValidationError:
            run.invalidate_recordset(["state", "page_count", "queue_job_uuid"])
            return self._finish_failure(
                run,
                job_uuid,
                classification="permanent",
                summary="Meta catalog page contract could not be applied.",
            )
        run.invalidate_recordset(["state", "page_count"])
        if run.state == "stale":
            return {"stale": True}
        return {
            "state": run.state,
            "stage": stage,
            "received": len(page.items),
            "has_more": page.has_more,
        }

    @api.model
    def _retry_or_finish(self, run, job_uuid, error):
        if self._job_attempt(job_uuid) < _CATALOG_ATTEMPT_CEILING:
            raise RetryableJobError(
                "Meta catalog is temporarily unavailable",
                seconds=getattr(error, "retry_after_seconds", 0) or None,
            ) from None
        return self._finish_failure(
            run,
            job_uuid,
            classification=getattr(error, "classification", "transient"),
            summary="Meta catalog exhausted its bounded retry policy.",
        )

    @api.model
    def _finish_failure(self, run, job_uuid, *, classification, summary):
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
        sync_service._transition(
            run,
            "failed",
            {
                "finished_at": fields.Datetime.now(),
                "error_class": self._safe_error_class(classification),
                "error_summary": summary,
            },
        )
        return {"state": "failed"}

    @api.model
    def _finish_paused(self, run, job_uuid, profile, error):
        if not self._lock_current_job(run, job_uuid):
            return {"orphan": True}
        meta_service = self.env["marketing.center.meta.service"]
        if not meta_service._lock_current_profile(
            profile,
            run.profile_revision,
            profile.meta_app_id.revision,
        ):
            return self._mark_stale_before_io(run, job_uuid)
        sync_service = self.env["marketing.center.sync.service"]
        if not sync_service._validate_fencing(run):
            return {"stale": True}
        # Lock order is run -> profile -> source -> connection.  Health projection
        # then reuses those locks and cannot race the catalog result fence.
        meta_service._mark_profile_failure(profile, error, paused=True)
        sync_service._transition(
            run,
            "stale",
            {
                "finished_at": fields.Datetime.now(),
                "error_class": "paused",
                "error_summary": "Meta catalog authorization is unavailable.",
            },
        )
        return {"stale": True, "profile_paused": True}

    @api.model
    def _mark_stale_before_io(self, run, job_uuid):
        if not self._lock_current_job(run, job_uuid):
            return {"orphan": True}
        sync_service = self.env["marketing.center.sync.service"]
        if not sync_service._validate_fencing(run):
            return {"stale": True}
        # The provider-specific profile is an additional fence not known by core.
        sync_service._transition(
            run,
            "stale",
            {
                "finished_at": fields.Datetime.now(),
                "error_summary": "Meta catalog profile changed before execution.",
            },
        )
        return {"stale": True}

    @api.model
    def _cursor_snapshot(self, run):
        cursor = (
            self.env["marketing.center.sync.cursor"]
            .sudo()
            .search(
                [
                    ("source_id", "=", run.source_id.id),
                    ("adapter_key", "=", run.adapter_key),
                    ("cursor_kind", "=", "catalog"),
                    ("entity_type", "=", run.entity_type),
                    ("grain", "=", False),
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
        """Replace a valid stale page job with the authoritative cursor job.

        A cursor can advance independently from the queued argument after a
        recovery or a competing transaction commits.  Returning silently here
        would leave the active run without a runnable owner until the watchdog
        closes it.  Serialize on the run and cursor, revalidate every fence, and
        atomically replace the current job UUID with the exact committed cursor
        sequence instead.
        """

        if not self._lock_current_job(run, job_uuid):
            return {"orphan": True}
        sync_service = self.env["marketing.center.sync.service"]
        profile = self._current_profile(run.connection_id, strict=False)
        meta_service = self.env["marketing.center.meta.service"]
        if (
            not profile
            or not meta_service._lock_current_profile(
                profile,
                run.profile_revision,
                profile.meta_app_id.revision,
            )
            or not self._preflight_current(run, profile)
        ):
            sync_service._transition(
                run,
                "stale",
                {
                    "finished_at": fields.Datetime.now(),
                    "error_summary": (
                        "Meta profile changed before cursor rescheduling."
                    ),
                },
            )
            return {"stale": True}
        if not sync_service._validate_fencing(run):
            return {"stale": True}
        cursor = sync_service._locked_cursor(run)
        cursor_sequence = cursor.cursor_sequence
        getattr(self, enqueue_method)(run, cursor_sequence)
        return {
            "cursor_changed": True,
            "rescheduled": True,
            "cursor_sequence": cursor_sequence,
        }

    @api.model
    def _current_profile(self, connection, *, strict=True):
        profile = connection.sudo().meta_profile_id.exists()
        valid = bool(
            profile
            and len(profile) == 1
            and profile.active
            and profile.meta_app_id.active
            and profile.company_id == connection.company_id
            and profile.public_ref == connection.profile_public_ref
            and profile.profile_revision == connection.profile_revision
        )
        if not valid:
            if strict:
                raise ValidationError(_("The Meta reader profile is not current."))
            return self.env["marketing.center.meta.profile"]
        return profile

    @api.model
    def _preflight_current(self, run, profile):
        source = run.source_id
        connection = run.connection_id
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
            and connection.adapter_key == run.adapter_key == META_ADAPTER_KEY
            and connection.binding_revision == run.binding_revision
            and connection.profile_revision == run.profile_revision
            and profile.profile_revision == run.profile_revision
            and profile.meta_app_id.active
        )

    @api.model
    def _meta_run(self, run, *, allow_terminal):
        run = run.sudo().exists()
        if (
            not run
            or run._name != "marketing.center.sync.run"
            or len(run) != 1
            or run.company_id not in self.env.companies
            or run.sync_kind != "catalog"
            or run.entity_type != META_CATALOG_RUN_ENTITY_TYPE
            or run.adapter_key != META_ADAPTER_KEY
            or run.source_id.service != META_ADS_SERVICE
        ):
            raise ValidationError(_("The Meta catalog synchronization run is invalid."))
        if not allow_terminal and run.state not in _ACTIVE_RUN_STATES:
            raise ValidationError(_("The Meta catalog synchronization is terminal."))
        return run

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
        allowed = {"paused", "permanent", "rate_limited", "transient"}
        return value if value in allowed else "permanent"

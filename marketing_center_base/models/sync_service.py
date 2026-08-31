import re

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.catalog_dto import (
    CatalogDTOValidationError,
    SyncPageDTO,
    canonical_json,
    sha256_text,
)
from ..services.tokens import MARKETING_SYNC_WRITE_TOKEN

_SAFE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_ALLOWED_TRANSITIONS = {
    "planned": {"queued", "running", "cancelled", "stale"},
    "queued": {"running", "cancelled", "stale", "failed"},
    "running": {"succeeded", "partial", "failed", "cancelled", "stale"},
    "succeeded": set(),
    "partial": set(),
    "failed": set(),
    "cancelled": set(),
    "stale": set(),
}


class MarketingCenterSyncService(models.AbstractModel):
    _name = "marketing.center.sync.service"
    _description = "Marketing Center Synchronization Service"

    @api.model
    def _plan_run(
        self,
        company,
        source,
        connection,
        *,
        sync_kind,
        scope_ref,
        trigger_kind,
        trigger_ref,
        entity_type="",
        grain="",
        reporting_context=None,
        window_start=None,
        window_end=None,
        report_timezone="",
    ):
        company, source = self.env["marketing.center.catalog.service"]._validated_scope(
            company, source
        )
        connection = connection.sudo().exists()
        if (
            not connection
            or len(connection) != 1
            or connection.source_id != source
            or connection.company_id != company
        ):
            raise ValidationError(_("The sync connection does not match the source."))
        self._lock_fencing_rows(source, connection)
        if not source.active or not source.read_enabled or source.state != "active":
            raise ValidationError(_("The marketing source is not enabled for reading."))
        if (
            not connection.active
            or connection.state != "ready"
            or connection.purpose != "reader"
        ):
            raise ValidationError(_("The marketing connection is not ready."))
        sync_kind = self._safe_key(sync_kind, "sync kind")
        if sync_kind not in {"catalog", "metrics", "leads"}:
            raise ValidationError(_("The synchronization kind is invalid."))
        entity_type = self._safe_key(entity_type, "entity type", required=False)
        grain = self._safe_key(grain, "grain", required=False)
        scope_ref = self._bounded_text(scope_ref, "scope reference", 512)
        trigger_kind = self._safe_key(trigger_kind, "trigger kind")
        if trigger_kind not in {"manual", "scheduled", "webhook", "backfill", "retry"}:
            raise ValidationError(_("The synchronization trigger is invalid."))
        trigger_ref = self._bounded_text(trigger_ref, "trigger reference", 512)
        window_start, window_end, report_timezone = self._normalized_window(
            window_start,
            window_end,
            report_timezone or source.timezone,
        )
        reporting_context = dict(reporting_context or {})
        try:
            context_json = canonical_json(reporting_context)
        except (TypeError, ValueError) as error:
            raise ValidationError(
                _("The reporting context must be valid JSON.")
            ) from error
        if len(context_json.encode("utf-8")) > 16 * 1024:
            raise ValidationError(_("The reporting context is too large."))
        reporting_context_hash = sha256_text(context_json)
        scope_hash = sha256_text(
            canonical_json(
                {
                    "sync_kind": sync_kind,
                    "entity_type": entity_type,
                    "grain": grain,
                    "scope_ref": scope_ref,
                    "reporting_context_hash": reporting_context_hash,
                }
            )
        )
        window_key = self._window_key(
            window_start,
            window_end,
            report_timezone,
        )
        request_fingerprint = sha256_text(
            canonical_json(
                {
                    "scope_hash": scope_hash,
                    "window_key": window_key,
                }
            )
        )
        run_key = sha256_text(
            canonical_json(
                {
                    "source": source.public_ref,
                    "sync_kind": sync_kind,
                    "scope_hash": scope_hash,
                    "trigger_kind": trigger_kind,
                    "trigger_ref": trigger_ref,
                }
            )
        )
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ["marketing_sync_scope:%s:%s:%s" % (source.id, sync_kind, scope_hash)],
        )
        run_model = self.env["marketing.center.sync.run"].sudo()
        existing = run_model.search(
            [("source_id", "=", source.id), ("run_key", "=", run_key)], limit=1
        )
        if existing:
            if (
                existing.request_fingerprint != request_fingerprint
                or existing.window_key != window_key
                or existing.connection_id != connection
                or existing.source_revision != source.configuration_revision
                or existing.binding_revision != connection.binding_revision
                or existing.profile_revision != connection.profile_revision
                or existing.adapter_key != connection.adapter_key
            ):
                raise ValidationError(
                    _("This synchronization occurrence conflicts with an existing run.")
                )
            return existing
        active_run = run_model.search(
            [
                ("source_id", "=", source.id),
                ("sync_kind", "=", sync_kind),
                ("scope_hash", "=", scope_hash),
                ("state", "in", ["planned", "queued", "running"]),
            ],
            limit=1,
        )
        if active_run:
            raise ValidationError(
                _("Synchronization run %s already owns this source scope.")
                % active_run.public_ref
            )
        return (
            run_model.with_company(company)
            .with_context(marketing_sync_write_token=MARKETING_SYNC_WRITE_TOKEN)
            .create(
                {
                    "company_id": company.id,
                    "source_id": source.id,
                    "connection_id": connection.id,
                    "source_revision": source.configuration_revision,
                    "binding_revision": connection.binding_revision,
                    "profile_revision": connection.profile_revision,
                    "adapter_key": connection.adapter_key,
                    "sync_kind": sync_kind,
                    "entity_type": entity_type or False,
                    "grain": grain or False,
                    "scope_ref": scope_ref,
                    "scope_hash": scope_hash,
                    "reporting_context_hash": reporting_context_hash,
                    "window_key": window_key,
                    "window_start": window_start or False,
                    "window_end": window_end or False,
                    "report_timezone": report_timezone,
                    "trigger_kind": trigger_kind,
                    "trigger_ref": trigger_ref,
                    "run_key": run_key,
                    "request_fingerprint": request_fingerprint,
                }
            )
        )

    @api.model
    def _apply_entity_page(self, run, payload, *, expected_cursor_sequence):
        run = self._validated_run(run, sync_kind="catalog")
        if (
            not isinstance(expected_cursor_sequence, int)
            or isinstance(expected_cursor_sequence, bool)
            or expected_cursor_sequence < 0
        ):
            raise ValidationError(
                _("The expected cursor sequence must be a non-negative integer.")
            )
        try:
            page = (
                payload
                if isinstance(payload, SyncPageDTO)
                else SyncPageDTO.from_dict(payload)
            )
        except CatalogDTOValidationError as error:
            raise ValidationError(
                _("Invalid synchronization page: %s") % error
            ) from error
        if page.reporting_context_hash != run.reporting_context_hash:
            raise ValidationError(
                _("The synchronization page reporting context does not match the run.")
            )
        if not self._validate_fencing(run):
            return run
        cursor = self._locked_cursor(run)
        if cursor.cursor_sequence != expected_cursor_sequence:
            raise ValidationError(_("The synchronization cursor changed concurrently."))
        if run.state in {"planned", "queued"}:
            self._transition(run, "running", {"started_at": fields.Datetime.now()})
        if page.provider_job_ref and page.provider_job_state in {"pending", "running"}:
            self._write_cursor(
                cursor,
                {
                    "provider_job_ref": page.provider_job_ref,
                    "watermark": page.watermark or cursor.watermark,
                    "cursor_sequence": cursor.cursor_sequence + 1,
                },
            )
            self._update_run_page(run, page, (), terminal=False)
            return run

        results = tuple(
            self.env["marketing.center.catalog.service"]._upsert_entity(
                run.company_id, run.source_id, item, sync_run=run
            )
            for item in page.items
        )
        cursor_values = {
            "cursor_value": page.next_cursor or False,
            "cursor_digest": sha256_text(page.next_cursor)
            if page.next_cursor
            else False,
            "cursor_sequence": cursor.cursor_sequence + 1,
            "provider_job_ref": page.provider_job_ref or False,
            "watermark": page.watermark or False,
            "last_success_run_id": run.id,
            "last_advanced_at": fields.Datetime.now(),
        }
        self._write_cursor(cursor, cursor_values)
        terminal = not page.has_more
        self._update_run_page(run, page, results, terminal=terminal)
        return run

    @api.model
    def _validated_run(self, run, sync_kind=None):
        if (
            not run
            or getattr(run, "_name", "") != "marketing.center.sync.run"
            or len(run) != 1
        ):
            raise ValidationError(_("A single synchronization run is required."))
        run = run.sudo().exists()
        if not run or run.company_id not in self.env.companies:
            raise AccessError(_("The synchronization run is not available."))
        if sync_kind and run.sync_kind != sync_kind:
            raise ValidationError(_("The synchronization run has the wrong kind."))
        if run.state not in {"planned", "queued", "running"}:
            raise ValidationError(_("The synchronization run is already terminal."))
        return run

    @api.model
    def _validate_fencing(self, run):
        source = run.source_id.sudo()
        connection = run.connection_id.sudo()
        self._lock_fencing_rows(source, connection)
        if (
            not source.active
            or not source.read_enabled
            or source.state != "active"
            or source.configuration_revision != run.source_revision
            or not connection.active
            or connection.state != "ready"
            or connection.purpose != "reader"
            or connection.source_id != source
            or connection.adapter_key != run.adapter_key
            or connection.binding_revision != run.binding_revision
            or connection.profile_revision != run.profile_revision
        ):
            self._transition(
                run,
                "stale",
                {
                    "finished_at": fields.Datetime.now(),
                    "error_summary": (
                        "Source or connection configuration changed before "
                        "execution."
                    ),
                },
            )
            return False
        return True

    @api.model
    def _locked_cursor(self, run):
        cursor_key = sha256_text(
            canonical_json(
                {
                    "adapter_key": run.adapter_key,
                    "sync_kind": run.sync_kind,
                    "entity_type": run.entity_type or "",
                    "grain": run.grain or "",
                    "scope_ref": run.scope_ref,
                    "reporting_context_hash": run.reporting_context_hash,
                    "window_key": run.window_key,
                }
            )
        )
        lock_key = "marketing_sync_cursor:%s:%s" % (run.source_id.id, cursor_key)
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [lock_key]
        )
        cursor_model = self.env["marketing.center.sync.cursor"].sudo()
        cursor = cursor_model.search(
            [("source_id", "=", run.source_id.id), ("cursor_key", "=", cursor_key)],
            limit=1,
        )
        if cursor:
            self.env.cr.execute(
                "SELECT id FROM marketing_center_sync_cursor WHERE id = %s FOR UPDATE",
                [cursor.id],
            )
            return cursor
        return (
            cursor_model.with_company(run.company_id)
            .with_context(marketing_sync_write_token=MARKETING_SYNC_WRITE_TOKEN)
            .create(
                {
                    "company_id": run.company_id.id,
                    "source_id": run.source_id.id,
                    "adapter_key": run.adapter_key,
                    "cursor_kind": run.sync_kind,
                    "entity_type": run.entity_type or False,
                    "grain": run.grain or False,
                    "scope_ref": run.scope_ref,
                    "reporting_context_hash": run.reporting_context_hash,
                    "window_key": run.window_key,
                    "cursor_key": cursor_key,
                }
            )
        )

    @api.model
    def _update_run_page(self, run, page, results, terminal):
        request_ids = list(run.provider_request_ids_json or [])
        if page.provider_request_id and page.provider_request_id not in request_ids:
            request_ids.append(page.provider_request_id)
        request_ids = request_ids[-64:]
        page_result_hash = sha256_text(
            canonical_json([result.content_hash for result in results])
        )
        result_hash = sha256_text(
            canonical_json(
                {
                    "previous": run.result_hash or "",
                    "page": page_result_hash,
                    "sequence": run.page_count + 1,
                }
            )
        )
        total_error_count = run.error_count + len(page.errors)
        values = {
            "page_count": run.page_count + 1,
            "received_count": run.received_count + len(page.items),
            "applied_count": run.applied_count
            + len([result for result in results if result.disposition != "duplicate"]),
            "duplicate_count": run.duplicate_count
            + len([result for result in results if result.disposition == "duplicate"]),
            "error_count": total_error_count,
            "provider_request_ids_json": request_ids,
            "result_hash": result_hash,
        }
        if terminal:
            values.update(
                {
                    "state": "partial" if total_error_count else "succeeded",
                    "finished_at": fields.Datetime.now(),
                }
            )
        self._write_run(run, values)

    @api.model
    def _transition(self, run, state, extra_values=None):
        if state not in _ALLOWED_TRANSITIONS.get(run.state, set()):
            raise ValidationError(
                _("Invalid synchronization transition from %s to %s.")
                % (run.state, state)
            )
        values = dict(extra_values or {})
        values["state"] = state
        self._write_run(run, values)

    @api.model
    def _write_run(self, run, values):
        run.with_context(marketing_sync_write_token=MARKETING_SYNC_WRITE_TOKEN).write(
            values
        )

    @api.model
    def _write_cursor(self, cursor, values):
        cursor.with_context(
            marketing_sync_write_token=MARKETING_SYNC_WRITE_TOKEN
        ).write(values)

    @api.model
    def _lock_fencing_rows(self, source, connection):
        self.env.cr.execute(
            "SELECT id FROM marketing_center_source WHERE id = %s FOR UPDATE",
            [source.id],
        )
        self.env.cr.execute(
            "SELECT id FROM marketing_center_connection WHERE id = %s FOR UPDATE",
            [connection.id],
        )
        source.invalidate_recordset(
            ["active", "read_enabled", "state", "configuration_revision"]
        )
        connection.invalidate_recordset(
            [
                "active",
                "state",
                "purpose",
                "source_id",
                "adapter_key",
                "binding_revision",
                "profile_revision",
            ]
        )

    @api.model
    def _normalized_window(
        self,
        window_start,
        window_end,
        report_timezone,
    ):
        if bool(window_start) != bool(window_end):
            raise ValidationError(
                _("Synchronization window start and end must be supplied together.")
            )
        try:
            start = fields.Datetime.to_datetime(window_start) if window_start else None
            end = fields.Datetime.to_datetime(window_end) if window_end else None
        except (TypeError, ValueError) as error:
            raise ValidationError(
                _("The synchronization window is invalid.")
            ) from error
        if start and start >= end:
            raise ValidationError(
                _("The synchronization window start must precede its end.")
            )
        timezone = self._bounded_text(
            report_timezone,
            "report timezone",
            64,
        )
        if timezone not in pytz.all_timezones_set:
            raise ValidationError(_("The report timezone is invalid."))
        return start, end, timezone

    @api.model
    def _window_key(self, window_start, window_end, report_timezone):
        return sha256_text(
            canonical_json(
                {
                    "report_timezone": report_timezone,
                    "window_end": fields.Datetime.to_string(window_end) or "",
                    "window_start": fields.Datetime.to_string(window_start) or "",
                }
            )
        )

    @api.model
    def _safe_key(self, value, label, required=True):
        value = self._bounded_text(value, label, 128, required=required).lower()
        if value and not _SAFE_KEY_RE.fullmatch(value):
            raise ValidationError(_("The %s is invalid.") % label)
        return value

    @api.model
    def _bounded_text(self, value, label, limit, required=True):
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValidationError(_("The %s must be text.") % label)
        value = value.strip()
        if required and not value:
            raise ValidationError(_("The %s is required.") % label)
        if len(value) > limit or any(ord(character) < 32 for character in value):
            raise ValidationError(_("The %s is invalid.") % label)
        return value

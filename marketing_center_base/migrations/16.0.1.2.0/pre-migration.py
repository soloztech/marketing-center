import hashlib
import json

ACTIVE_RUN_STATES = ("planned", "queued", "running")


def _sha256_json(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _datetime_text(value):
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""


def _window_key(window_start, window_end, report_timezone):
    return _sha256_json(
        {
            "report_timezone": report_timezone or "UTC",
            "window_end": _datetime_text(window_end),
            "window_start": _datetime_text(window_start),
        }
    )


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", [table])
    return bool(cr.fetchone()[0])


def _prepare_run_fencing(cr):
    table = "marketing_center_sync_run"
    if not _table_exists(cr, table):
        return
    cr.execute(
        "ALTER TABLE marketing_center_sync_run "
        "ADD COLUMN IF NOT EXISTS source_revision integer, "
        "ADD COLUMN IF NOT EXISTS window_key varchar"
    )
    cr.execute(
        "SELECT run.id, run.window_start, run.window_end, run.report_timezone, "
        "source.configuration_revision "
        "FROM marketing_center_sync_run run "
        "JOIN marketing_center_source source ON source.id = run.source_id "
        "ORDER BY run.id"
    )
    for run_id, window_start, window_end, timezone, source_revision in cr.fetchall():
        cr.execute(
            "UPDATE marketing_center_sync_run "
            "SET source_revision = %s, window_key = %s WHERE id = %s",
            [
                source_revision,
                _window_key(window_start, window_end, timezone),
                run_id,
            ],
        )
    cr.execute(
        "UPDATE marketing_center_sync_run "
        "SET state = 'stale', finished_at = COALESCE(finished_at, NOW()), "
        "error_summary = %s WHERE state IN %s",
        ["Legacy run fenced during the 1.2.0 upgrade.", ACTIVE_RUN_STATES],
    )


def _prepare_cursor_windows(cr):
    table = "marketing_center_sync_cursor"
    if not _table_exists(cr, table):
        return
    cr.execute(
        "ALTER TABLE marketing_center_sync_cursor "
        "ADD COLUMN IF NOT EXISTS window_key varchar"
    )
    cr.execute(
        "SELECT cursor.id, cursor.source_id, cursor.adapter_key, "
        "cursor.cursor_kind, cursor.entity_type, cursor.grain, cursor.scope_ref, "
        "cursor.reporting_context_hash, run.window_key "
        "FROM marketing_center_sync_cursor cursor "
        "LEFT JOIN marketing_center_sync_run run "
        "ON run.id = cursor.last_success_run_id ORDER BY cursor.id"
    )
    keys = set()
    for row in cr.fetchall():
        (
            cursor_id,
            source_id,
            adapter_key,
            cursor_kind,
            entity_type,
            grain,
            scope_ref,
            reporting_context_hash,
            run_window_key,
        ) = row
        window_key = run_window_key or _sha256_json(
            {"legacy_cursor_id": cursor_id, "migration": "16.0.1.2.0"}
        )
        cursor_key = _sha256_json(
            {
                "adapter_key": adapter_key,
                "entity_type": entity_type or "",
                "grain": grain or "",
                "reporting_context_hash": reporting_context_hash,
                "scope_ref": scope_ref,
                "sync_kind": cursor_kind,
                "window_key": window_key,
            }
        )
        identity = (source_id, cursor_key)
        if identity in keys:
            raise RuntimeError(
                "duplicate synchronization cursors found during 1.2.0 upgrade"
            )
        keys.add(identity)
        cr.execute(
            "UPDATE marketing_center_sync_cursor "
            "SET window_key = %s, cursor_key = %s WHERE id = %s",
            [window_key, cursor_key, cursor_id],
        )


def _drop_legacy_truncated_objects(cr):
    table = "marketing_center_external_entity_revision"
    if not _table_exists(cr, table):
        return
    cr.execute(
        "ALTER TABLE marketing_center_external_entity_revision "
        "DROP CONSTRAINT IF EXISTS "
        "marketing_center_external_entity_revision_entity_revision_uniqu, "
        "DROP CONSTRAINT IF EXISTS "
        "marketing_center_external_entity_revision_revision_sequence_pos"
    )
    for index_name in (
        "marketing_center_external_entity_current_revision_sequence_inde",
        "marketing_center_external_entity_revision_revision_sequence_ind",
        "marketing_center_external_entity_revision_provider_updated_at_i",
        "marketing_center_external_entity_revision_remote_missing_at_ind",
    ):
        cr.execute('DROP INDEX IF EXISTS "%s"' % index_name)


def migrate(cr, version):
    del version
    _prepare_run_fencing(cr)
    _prepare_cursor_windows(cr)
    _drop_legacy_truncated_objects(cr)

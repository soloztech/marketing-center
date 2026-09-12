"""Preserve existing source identities before source links become optional."""

from psycopg2 import sql


def migrate(cr, version):
    mappings = {
        "marketing_contact_center_response_signal": {
            "source_message_res_id": "message_binding_id",
            "source_delivery_res_id": "delivery_event_id",
        },
        "marketing_contact_center_response_episode": {
            "source_message_res_id": "start_message_binding_id",
        },
        "marketing_contact_center_response": {
            "source_message_res_id": "message_binding_id",
            "source_delivery_res_id": "delivery_event_id",
        },
        "marketing_contact_center_response_cursor": {
            "last_message_res_id": "last_message_binding_id",
            "backfill_cutoff_message_res_id": "backfill_cutoff_message_binding_id",
            "backfill_after_message_res_id": "backfill_after_message_binding_id",
        },
    }
    # Identifiers are constants owned by this migration, never external input.
    # This copies only integers. No fact, timestamp, payload or link is removed.
    for table, columns in mappings.items():
        for target, source in columns.items():
            cr.execute(
                sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} integer").format(
                    sql.Identifier(table), sql.Identifier(target)
                )
            )
            cr.execute(
                sql.SQL("UPDATE {} SET {} = COALESCE({}, 0) WHERE {} IS NULL").format(
                    sql.Identifier(table),
                    sql.Identifier(target),
                    sql.Identifier(source),
                    sql.Identifier(target),
                )
            )

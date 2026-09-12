"""Keep marketing facts when the Contact Center expires their source content."""

from odoo import fields, models

from ..services.tokens import (
    MARKETING_CONTACT_CENTER_EPISODE_WRITE_TOKEN,
    MARKETING_CONTACT_CENTER_RETENTION_TOKEN,
)


class ContactCenterRetention(models.AbstractModel):
    _inherit = "contact.center.retention"

    def _retention_prepare_external_references(
        self, binding, messages, message_bindings, inbox_events
    ):
        result = super()._retention_prepare_external_references(
            binding, messages, message_bindings, inbox_events
        )
        if result is False or not message_bindings:
            return result
        service = self.env["marketing.contact.center.response.episode.service"].sudo()
        # Match the existing projection lock order: episode, then lifecycle.
        # A busy advisory key aborts this batch for a fresh transaction.
        service._lock_channel(binding)
        lifecycle = self.env["marketing.contact.center.lifecycle.service"].sudo()
        for kind in ("conversation_started", "first_human_response"):
            lifecycle._sync_event(binding, kind)
        service._record_signals(message_bindings)
        progress = service._reconcile_channel(binding, page_size=200)
        if progress["has_more"]:
            cursor = service._cursor(binding)
            targets = (
                self.env["marketing.contact.center.response.signal"]
                .sudo()
                .search([("message_binding_id", "in", message_bindings.ids)])
            )
            frontier = (
                cursor.last_observed_at,
                cursor.last_message_res_id,
                cursor.last_signal_id.id,
            )
            # An active group can receive new messages faster than a whole
            # timeline drains. Only this deletion batch must be fully consumed.
            if cursor.backfill_state == "materializing" or any(
                not frontier[0]
                or (row.observed_at, row.source_message_res_id, row.id) > frontier
                for row in targets
            ):
                return False

        context = {
            "marketing_contact_center_episode_write_token": (
                MARKETING_CONTACT_CENTER_EPISODE_WRITE_TOKEN
            ),
            "marketing_contact_center_retention_token": (
                MARKETING_CONTACT_CENTER_RETENTION_TOKEN
            ),
        }
        now = fields.Datetime.now()
        for model, link in (
            ("marketing.contact.center.response.signal", "message_binding_id"),
            ("marketing.contact.center.response.episode", "start_message_binding_id"),
            ("marketing.contact.center.response", "message_binding_id"),
        ):
            rows = self.env[model].sudo().search([(link, "in", message_bindings.ids)])
            for row in rows:
                source = row[link]
                delivery_id = (
                    row.delivery_event_id.id
                    if "delivery_event_id" in row._fields
                    else False
                )
                values = row._snapshot_values(source, delivery_id)
                values.update({link: False, "source_expired_at": now})
                if "delivery_event_id" in row._fields:
                    values["delivery_event_id"] = False
                row.with_context(**context).write(values)

        cursors = (
            self.env["marketing.contact.center.response.cursor"]
            .sudo()
            .search([("channel_binding_id", "=", binding.id)])
        )
        target_ids = set(message_bindings.ids)
        for cursor in cursors:
            values = {}
            for link in (
                "last_message_binding_id",
                "backfill_cutoff_message_binding_id",
                "backfill_after_message_binding_id",
            ):
                if cursor[link].id in target_ids:
                    values[link] = False
            if values:
                cursor.with_context(**context).write(values)
        return True

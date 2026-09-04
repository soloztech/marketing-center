import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.tokens import MARKETING_SYNC_WRITE_TOKEN


def _uuid(_recordset):
    return str(uuid.uuid4())


class MarketingCenterSyncRun(models.Model):
    _name = "marketing.center.sync.run"
    _description = "Marketing Center Synchronization Run"
    _order = "create_date desc, id desc"
    _rec_name = "public_ref"
    _check_company_auto = True

    public_ref = fields.Char(
        required=True,
        default=_uuid,
        size=36,
        index=True,
        readonly=True,
        copy=False,
    )
    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict", readonly=True
    )
    source_id = fields.Many2one(
        "marketing.center.source",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    connection_id = fields.Many2one(
        "marketing.center.connection",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    source_revision = fields.Integer(
        required=True,
        readonly=True,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    binding_revision = fields.Integer(
        required=True,
        readonly=True,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    profile_revision = fields.Integer(
        required=True,
        readonly=True,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    adapter_key = fields.Char(
        required=True,
        size=128,
        index=True,
        readonly=True,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    sync_kind = fields.Selection(
        [
            ("catalog", "External entities"),
            ("metrics", "Performance metrics"),
            ("leads", "Lead acquisition"),
        ],
        required=True,
        index=True,
        readonly=True,
    )
    entity_type = fields.Char(size=128, index=True, readonly=True)
    grain = fields.Char(size=128, index=True, readonly=True)
    scope_ref = fields.Char(required=True, size=512, index=True, readonly=True)
    scope_hash = fields.Char(required=True, size=64, index=True, readonly=True)
    reporting_context_hash = fields.Char(
        required=True, size=64, index=True, readonly=True
    )
    reporting_context_json = fields.Json(
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    window_key = fields.Char(required=True, size=64, index=True, readonly=True)
    window_start = fields.Datetime(index=True, readonly=True)
    window_end = fields.Datetime(index=True, readonly=True)
    report_timezone = fields.Char(size=64, readonly=True)
    trigger_kind = fields.Selection(
        [
            ("manual", "Manual"),
            ("scheduled", "Scheduled"),
            ("webhook", "Webhook"),
            ("backfill", "Backfill"),
            ("retry", "Retry"),
        ],
        required=True,
        index=True,
        readonly=True,
    )
    trigger_ref = fields.Char(required=True, size=512, index=True, readonly=True)
    run_key = fields.Char(required=True, size=64, index=True, readonly=True)
    request_fingerprint = fields.Char(required=True, size=64, index=True, readonly=True)
    state = fields.Selection(
        [
            ("planned", "Planned"),
            ("queued", "Queued"),
            ("running", "Running"),
            ("succeeded", "Succeeded"),
            ("partial", "Partial"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
            ("stale", "Stale"),
        ],
        required=True,
        default="planned",
        index=True,
        readonly=True,
    )
    queue_job_uuid = fields.Char(
        size=36,
        index=True,
        readonly=True,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    deferred_until = fields.Datetime(
        index=True,
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_admin",
        help=(
            "Earliest expected execution time for an intentionally deferred "
            "successor. The watchdog treats this as valid progress."
        ),
    )
    started_at = fields.Datetime(index=True, readonly=True)
    finished_at = fields.Datetime(index=True, readonly=True)
    page_count = fields.Integer(required=True, default=0, readonly=True)
    received_count = fields.Integer(required=True, default=0, readonly=True)
    applied_count = fields.Integer(required=True, default=0, readonly=True)
    duplicate_count = fields.Integer(required=True, default=0, readonly=True)
    error_count = fields.Integer(required=True, default=0, readonly=True)
    result_hash = fields.Char(size=64, index=True, readonly=True)
    provider_request_ids_json = fields.Json(
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    error_class = fields.Char(
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    error_summary = fields.Char(readonly=True, copy=False)

    _sql_constraints = [
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The synchronization run public reference must be unique.",
        ),
        (
            "source_run_key_unique",
            "unique(source_id, run_key)",
            "This synchronization occurrence already exists.",
        ),
        (
            "queue_job_uuid_unique",
            "unique(queue_job_uuid)",
            "This queue job is already attached to a synchronization run.",
        ),
        (
            "run_hashes_sha256",
            "check(char_length(run_key) = 64 and char_length(scope_hash) = 64 "
            "and char_length(reporting_context_hash) = 64 "
            "and char_length(window_key) = 64 "
            "and char_length(request_fingerprint) = 64 "
            "and (result_hash is null or char_length(result_hash) = 64))",
            "Synchronization run hashes must be SHA-256 digests.",
        ),
        (
            "run_counters_nonnegative",
            "check(page_count >= 0 and received_count >= 0 and applied_count >= 0 "
            "and duplicate_count >= 0 and error_count >= 0)",
            "Synchronization run counters cannot be negative.",
        ),
        (
            "run_revisions_nonnegative",
            "check(source_revision > 0 and binding_revision > 0 "
            "and profile_revision >= 0)",
            "Synchronization fencing revisions are invalid.",
        ),
    ]

    def init(self):
        self.env.cr.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS mc_sync_run_active_uq "
            "ON marketing_center_sync_run (source_id, sync_kind, scope_hash) "
            "WHERE state IN ('planned', 'queued', 'running')"
        )

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("marketing_sync_write_token")
            is not MARKETING_SYNC_WRITE_TOKEN
        ):
            raise AccessError(
                _("Synchronization runs are created only by the service.")
            )
        return super().create(vals_list)

    def write(self, values):
        if (
            self.env.context.get("marketing_sync_write_token")
            is not MARKETING_SYNC_WRITE_TOKEN
        ):
            raise AccessError(
                _("Synchronization runs are updated only by the service.")
            )
        return super().write(values)

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Synchronization runs cannot be deleted."))

    def action_cancel(self):
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_admin"
        ):
            raise AccessError(
                _("Only Marketing Center administrators can cancel synchronizations.")
            )
        self.check_access_rights("read")
        self.check_access_rule("read")
        for run in self:
            self.env["marketing.center.sync.service"]._cancel_run(run)
        return True

    def action_open_queue_job(self):
        self.ensure_one()
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(
                _("Only Settings administrators can inspect synchronization jobs.")
            )
        if not self.queue_job_uuid or "queue.job" not in self.env:
            raise ValidationError(_("This synchronization has no available queue job."))
        job = (
            self.env["queue.job"]
            .sudo()
            .search([("uuid", "=", self.queue_job_uuid)], limit=1)
        )
        if not job:
            raise ValidationError(_("The queue job is no longer available."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Queue Job"),
            "res_model": "queue.job",
            "res_id": job.id,
            "view_mode": "form",
            "target": "current",
        }

    @api.constrains("company_id", "source_id", "connection_id")
    def _check_scope_links(self):
        for run in self:
            if (
                run.source_id.company_id != run.company_id
                or run.connection_id.company_id != run.company_id
                or run.connection_id.source_id != run.source_id
            ):
                raise ValidationError(
                    _("A synchronization run cannot cross company or source scope.")
                )


class MarketingCenterSyncCursor(models.Model):
    _name = "marketing.center.sync.cursor"
    _description = "Marketing Center Synchronization Cursor"
    _order = "source_id, cursor_kind, scope_ref, id"
    _rec_name = "scope_ref"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict", readonly=True
    )
    source_id = fields.Many2one(
        "marketing.center.source",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    adapter_key = fields.Char(required=True, size=128, index=True, readonly=True)
    cursor_kind = fields.Selection(
        [
            ("catalog", "External entities"),
            ("metrics", "Performance metrics"),
            ("leads", "Lead acquisition"),
        ],
        required=True,
        index=True,
        readonly=True,
    )
    entity_type = fields.Char(size=128, index=True, readonly=True)
    grain = fields.Char(size=128, index=True, readonly=True)
    scope_ref = fields.Char(required=True, size=512, index=True, readonly=True)
    reporting_context_hash = fields.Char(
        required=True, size=64, index=True, readonly=True
    )
    window_key = fields.Char(required=True, size=64, index=True, readonly=True)
    cursor_key = fields.Char(required=True, size=64, index=True, readonly=True)
    cursor_value = fields.Text(
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    cursor_digest = fields.Char(
        size=64,
        index=True,
        readonly=True,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    cursor_sequence = fields.Integer(required=True, default=0, readonly=True)
    provider_job_ref = fields.Text(
        readonly=True,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    watermark = fields.Text(readonly=True)
    last_success_run_id = fields.Many2one(
        "marketing.center.sync.run",
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    last_advanced_at = fields.Datetime(index=True, readonly=True)

    _sql_constraints = [
        (
            "source_cursor_key_unique",
            "unique(source_id, cursor_key)",
            "This synchronization cursor already exists.",
        ),
        (
            "cursor_hashes_sha256",
            "check(char_length(cursor_key) = 64 "
            "and char_length(reporting_context_hash) = 64 "
            "and char_length(window_key) = 64 "
            "and (cursor_digest is null or char_length(cursor_digest) = 64))",
            "Synchronization cursor hashes must be SHA-256 digests.",
        ),
        (
            "cursor_sequence_nonnegative",
            "check(cursor_sequence >= 0)",
            "The synchronization cursor sequence cannot be negative.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("marketing_sync_write_token")
            is not MARKETING_SYNC_WRITE_TOKEN
        ):
            raise AccessError(_("Sync cursors are created only by the service."))
        return super().create(vals_list)

    def write(self, values):
        if (
            self.env.context.get("marketing_sync_write_token")
            is not MARKETING_SYNC_WRITE_TOKEN
        ):
            raise AccessError(_("Sync cursors are advanced only by the service."))
        return super().write(values)

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Synchronization cursors cannot be deleted."))

    @api.constrains("company_id", "source_id", "last_success_run_id", "window_key")
    def _check_scope_links(self):
        for cursor in self:
            run = cursor.last_success_run_id
            if cursor.source_id.company_id != cursor.company_id or (
                run
                and (
                    run.company_id != cursor.company_id
                    or run.source_id != cursor.source_id
                    or run.window_key != cursor.window_key
                )
            ):
                raise ValidationError(
                    _(
                        "A synchronization cursor cannot cross company, source, "
                        "or window scope."
                    )
                )

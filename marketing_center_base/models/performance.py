import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.tokens import MARKETING_PERFORMANCE_WRITE_TOKEN


def _uuid(_recordset):
    return str(uuid.uuid4())


class BigInteger(fields.Integer):
    """PostgreSQL BIGINT with Odoo's normal integer cache semantics."""

    column_type = ("int8", "int8")


class MarketingCenterMetricDaily(models.Model):
    _name = "marketing.center.metric.daily"
    _description = "Marketing Center Daily Performance Metric"
    _order = "report_date desc, source_id, grain, entity_external_ref, id"
    _rec_name = "entity_external_ref"
    _check_company_auto = True

    public_ref = fields.Char(
        required=True,
        default=_uuid,
        size=36,
        index=True,
        readonly=True,
        copy=False,
    )
    source_id = fields.Many2one(
        "marketing.center.source",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    company_id = fields.Many2one(
        related="source_id.company_id", store=True, readonly=True, index=True
    )
    grain = fields.Selection(
        [("account", "Account"), ("campaign", "Campaign")],
        required=True,
        index=True,
        readonly=True,
    )
    entity_external_ref = fields.Char(
        required=True, size=1024, index=True, readonly=True
    )
    entity_id = fields.Many2one(
        "marketing.center.external.entity",
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    report_date = fields.Date(required=True, index=True, readonly=True)
    period_start_utc = fields.Datetime(required=True, index=True, readonly=True)
    period_end_utc = fields.Datetime(required=True, index=True, readonly=True)
    report_timezone = fields.Char(required=True, size=64, readonly=True)
    currency = fields.Char(required=True, size=3, index=True, readonly=True)
    metric_origin = fields.Selection(
        [("platform_reported", "Platform reported")],
        required=True,
        index=True,
        readonly=True,
    )
    dimension_hash = fields.Char(required=True, size=64, index=True, readonly=True)
    # Odoo 16 serializes an empty JSON object as SQL NULL.  The immutable
    # dimension_hash remains the canonical representation of ``{}``, while
    # non-empty controlled dimensions are persisted normally.
    dimensions_json = fields.Json(readonly=True, copy=False)
    reporting_context_hash = fields.Char(
        required=True, size=64, index=True, readonly=True
    )
    has_impressions = fields.Boolean(required=True, default=False, readonly=True)
    impressions = BigInteger(
        required=True, default=0, readonly=True, group_operator=False
    )
    has_clicks = fields.Boolean(required=True, default=False, readonly=True)
    clicks = BigInteger(required=True, default=0, readonly=True, group_operator=False)
    has_cost_micros = fields.Boolean(required=True, default=False, readonly=True)
    cost_micros = BigInteger(
        required=True, default=0, readonly=True, group_operator=False
    )
    first_observed_at = fields.Datetime(required=True, index=True, readonly=True)
    last_observed_at = fields.Datetime(required=True, index=True, readonly=True)
    current_content_hash = fields.Char(
        required=True, size=64, index=True, readonly=True
    )
    current_revision_sequence = fields.Integer(required=True, default=0, readonly=True)
    current_revision_id = fields.Many2one(
        "marketing.center.metric.revision",
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    revision_ids = fields.One2many(
        "marketing.center.metric.revision", "metric_id", readonly=True
    )
    last_sync_run_id = fields.Many2one(
        "marketing.center.sync.run",
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    last_sync_finished_at = fields.Datetime(
        related="last_sync_run_id.finished_at", readonly=True
    )
    last_sync_state = fields.Selection(related="last_sync_run_id.state", readonly=True)

    _sql_constraints = [
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The performance metric public reference must be unique.",
        ),
        (
            "metric_identity_unique",
            "unique(source_id, report_date, grain, entity_external_ref, "
            "dimension_hash, metric_origin, reporting_context_hash)",
            "This daily performance metric already exists.",
        ),
        (
            "metric_hashes_sha256",
            "check(char_length(current_content_hash) = 64 "
            "and char_length(dimension_hash) = 64 "
            "and char_length(reporting_context_hash) = 64)",
            "Performance metric hashes must be SHA-256 digests.",
        ),
        (
            "metric_sequence_nonnegative",
            "check(current_revision_sequence >= 0)",
            "The current metric revision sequence cannot be negative.",
        ),
        (
            "metric_counters_nonnegative",
            "check(impressions >= 0 and clicks >= 0 and cost_micros >= 0 "
            "and (has_impressions or has_clicks or has_cost_micros) "
            "and (has_impressions or impressions = 0) "
            "and (has_clicks or clicks = 0) "
            "and (has_cost_micros or cost_micros = 0))",
            "Performance values cannot be negative.",
        ),
        (
            "metric_period_order",
            "check(period_start_utc < period_end_utc)",
            "The metric period start must precede its end.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("marketing_performance_write_token")
            is not MARKETING_PERFORMANCE_WRITE_TOKEN
        ):
            raise AccessError(
                _("Performance metrics are created only by performance sync.")
            )
        return super().create(vals_list)

    def write(self, values):
        if (
            self.env.context.get("marketing_performance_write_token")
            is not MARKETING_PERFORMANCE_WRITE_TOKEN
        ):
            raise AccessError(
                _("Performance metrics are updated only by performance sync.")
            )
        return super().write(values)

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Performance metrics cannot be deleted."))

    @api.constrains("entity_id", "source_id", "grain", "entity_external_ref")
    def _check_entity_scope(self):
        for metric in self:
            entity = metric.entity_id
            if entity and (
                entity.source_id != metric.source_id
                or entity.entity_type != metric.grain
                or entity.external_ref != metric.entity_external_ref
            ):
                raise ValidationError(
                    _("The performance entity must match its source and identity.")
                )

    @api.constrains("current_revision_id")
    def _check_current_revision(self):
        for metric in self:
            if (
                metric.current_revision_id
                and metric.current_revision_id.metric_id != metric
            ):
                raise ValidationError(
                    _("The current revision must belong to its performance metric.")
                )

    @api.constrains(
        "last_sync_run_id",
        "source_id",
        "grain",
        "reporting_context_hash",
        "period_start_utc",
        "period_end_utc",
    )
    def _check_last_sync_scope(self):
        for metric in self:
            run = metric.last_sync_run_id
            if run and (
                run.source_id != metric.source_id
                or run.sync_kind != "metrics"
                or run.grain != metric.grain
                or run.reporting_context_hash != metric.reporting_context_hash
                or not run.window_start
                or not run.window_end
                or metric.period_start_utc < run.window_start
                or metric.period_end_utc > run.window_end
            ):
                raise ValidationError(
                    _("The last synchronization run does not cover this metric.")
                )


class MarketingCenterMetricRevision(models.Model):
    _name = "marketing.center.metric.revision"
    _description = "Marketing Center Performance Metric Revision"
    _order = "metric_id, revision_sequence desc, id desc"
    _rec_name = "content_hash"
    _check_company_auto = True

    metric_id = fields.Many2one(
        "marketing.center.metric.daily",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    source_id = fields.Many2one(
        related="metric_id.source_id", store=True, readonly=True, index=True
    )
    company_id = fields.Many2one(
        related="metric_id.company_id", store=True, readonly=True, index=True
    )
    revision_sequence = fields.Integer(required=True, readonly=True)
    content_hash = fields.Char(required=True, size=64, index=True, readonly=True)
    schema_version = fields.Integer(required=True, readonly=True)
    observed_at = fields.Datetime(required=True, index=True, readonly=True)
    sync_run_id = fields.Many2one(
        "marketing.center.sync.run",
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    has_impressions = fields.Boolean(required=True, default=False, readonly=True)
    impressions = BigInteger(
        required=True, default=0, readonly=True, group_operator=False
    )
    has_clicks = fields.Boolean(required=True, default=False, readonly=True)
    clicks = BigInteger(required=True, default=0, readonly=True, group_operator=False)
    has_cost_micros = fields.Boolean(required=True, default=False, readonly=True)
    cost_micros = BigInteger(
        required=True, default=0, readonly=True, group_operator=False
    )
    snapshot_json = fields.Json(
        required=True,
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_admin",
    )

    _sql_constraints = [
        (
            "metric_revision_unique",
            "unique(metric_id, revision_sequence)",
            "This metric revision sequence already exists.",
        ),
        (
            "metric_revision_hash_sha256",
            "check(char_length(content_hash) = 64)",
            "The metric revision content hash must be a SHA-256 digest.",
        ),
        (
            "metric_revision_sequence_positive",
            "check(revision_sequence > 0)",
            "The metric revision sequence must be positive.",
        ),
        (
            "metric_revision_counters_nonnegative",
            "check(impressions >= 0 and clicks >= 0 and cost_micros >= 0 "
            "and (has_impressions or has_clicks or has_cost_micros) "
            "and (has_impressions or impressions = 0) "
            "and (has_clicks or clicks = 0) "
            "and (has_cost_micros or cost_micros = 0))",
            "Performance revision values cannot be negative.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("marketing_performance_write_token")
            is not MARKETING_PERFORMANCE_WRITE_TOKEN
        ):
            raise AccessError(
                _("Metric revisions are created only by performance sync.")
            )
        return super().create(vals_list)

    def write(self, values):  # pylint: disable=method-required-super
        raise AccessError(_("Performance metric revisions cannot be edited."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Performance metric revisions cannot be deleted."))

    @api.constrains("sync_run_id", "metric_id")
    def _check_sync_scope(self):
        for revision in self:
            run = revision.sync_run_id
            metric = revision.metric_id
            if run and (
                run.source_id != metric.source_id
                or run.sync_kind != "metrics"
                or run.grain != metric.grain
                or run.reporting_context_hash != metric.reporting_context_hash
                or not run.window_start
                or not run.window_end
                or metric.period_start_utc < run.window_start
                or metric.period_end_utc > run.window_end
            ):
                raise ValidationError(
                    _("The synchronization run does not cover this metric revision.")
                )

from odoo import _, api, models
from odoo.exceptions import ValidationError

from ..services.performance_dto import (
    MarketingPerformanceDTO,
    PerformanceDTOValidationError,
    PerformanceIngestResult,
)
from ..services.serialization import acquire_advisory_xact_lock
from ..services.tokens import MARKETING_PERFORMANCE_WRITE_TOKEN


class MarketingCenterPerformanceService(models.AbstractModel):
    _name = "marketing.center.performance.service"
    _description = "Marketing Center Performance Service"

    @api.model
    def _upsert_metric(self, company, source, payload, sync_run=None):
        catalog_service = self.env["marketing.center.catalog.service"]
        _company_id, source_id = catalog_service._scope_ids_for_lock(company, source)
        try:
            dto = (
                payload
                if isinstance(payload, MarketingPerformanceDTO)
                else MarketingPerformanceDTO.from_dict(payload)
            )
        except PerformanceDTOValidationError as error:
            raise ValidationError(
                _("Invalid performance metric: %s") % error
            ) from error

        lock_key = "marketing_performance:%s:%s" % (source_id, dto.canonical_key)
        acquire_advisory_xact_lock(
            self.env.cr,
            lock_key,
            "Concurrent marketing metric ingestion requires a fresh snapshot",
        )
        company, source = catalog_service._validated_scope(company, source)
        sync_run = sync_run.sudo().exists() if sync_run else sync_run
        if sync_run and (
            len(sync_run) != 1
            or sync_run.source_id != source
            or sync_run.company_id != company
            or sync_run.sync_kind != "metrics"
        ):
            raise ValidationError(_("The sync run does not match the metric source."))
        self._validate_metric_scope(source, dto, sync_run)
        metric_model = self.env["marketing.center.metric.daily"].sudo()
        metric = metric_model.search(
            [
                ("source_id", "=", source.id),
                ("report_date", "=", dto.report_date),
                ("grain", "=", dto.grain),
                ("entity_external_ref", "=", dto.entity_external_ref),
                ("dimension_hash", "=", dto.dimension_hash),
                ("metric_origin", "=", dto.metric_origin),
                ("reporting_context_hash", "=", dto.reporting_context_hash),
            ],
            limit=1,
        )
        entity = self._resolve_entity(source, dto)
        if metric and metric.current_content_hash == dto.content_hash:
            values = {
                "last_observed_at": max(metric.last_observed_at, dto.observed_at),
                "last_sync_run_id": (
                    sync_run.id if sync_run else metric.last_sync_run_id.id
                ),
            }
            if entity and metric.entity_id != entity:
                values["entity_id"] = entity.id
            self._write_metric(metric, values)
            return self._result(metric, "duplicate")

        if not metric:
            metric = self._create_metric(company, source, dto, entity, sync_run)
        sequence = metric.current_revision_sequence + 1
        revision = self._create_revision(metric, dto, sequence, sync_run)
        self._write_metric(
            metric,
            {
                "entity_id": entity.id if entity else False,
                "period_start_utc": dto.period_start_utc,
                "period_end_utc": dto.period_end_utc,
                "report_timezone": dto.report_timezone,
                "currency": dto.currency,
                "dimensions_json": dto.dimensions,
                **self._metric_values(dto),
                "last_observed_at": max(metric.last_observed_at, dto.observed_at),
                "current_content_hash": dto.content_hash,
                "current_revision_sequence": sequence,
                "current_revision_id": revision.id,
                "last_sync_run_id": sync_run.id if sync_run else False,
            },
        )
        return self._result(metric, "updated" if sequence > 1 else "accepted")

    @api.model
    def _validate_metric_scope(self, source, dto, sync_run):
        if dto.report_timezone != source.timezone:
            raise ValidationError(
                _("The metric timezone does not match the marketing source.")
            )
        if dto.currency != source.currency_id.name.upper():
            raise ValidationError(
                _("The metric currency does not match the marketing source.")
            )
        if not sync_run:
            return
        if sync_run.grain != dto.grain:
            raise ValidationError(_("The metric grain does not match the sync run."))
        if sync_run.reporting_context_hash != dto.reporting_context_hash:
            raise ValidationError(
                _("The metric reporting context does not match the sync run.")
            )
        if sync_run.report_timezone != dto.report_timezone:
            raise ValidationError(
                _("The metric report timezone does not match the sync run.")
            )
        if (
            not sync_run.window_start
            or not sync_run.window_end
            or dto.period_start_utc < sync_run.window_start
            or dto.period_end_utc > sync_run.window_end
        ):
            raise ValidationError(_("The metric is outside the sync run window."))

    @api.model
    def _resolve_entity(self, source, dto):
        return (
            self.env["marketing.center.external.entity"]
            .sudo()
            .search(
                [
                    ("source_id", "=", source.id),
                    ("entity_type", "=", dto.grain),
                    ("external_ref", "=", dto.entity_external_ref),
                ],
                limit=1,
            )
        )

    @api.model
    def _metric_values(self, dto):
        return {
            "has_impressions": dto.impressions is not None,
            "impressions": dto.impressions if dto.impressions is not None else 0,
            "has_clicks": dto.clicks is not None,
            "clicks": dto.clicks if dto.clicks is not None else 0,
            "has_cost_micros": dto.cost_micros is not None,
            "cost_micros": dto.cost_micros if dto.cost_micros is not None else 0,
        }

    @api.model
    def _create_metric(self, company, source, dto, entity, sync_run):
        return (
            self.env["marketing.center.metric.daily"]
            .sudo()
            .with_company(company)
            .with_context(
                marketing_performance_write_token=MARKETING_PERFORMANCE_WRITE_TOKEN
            )
            .create(
                {
                    "source_id": source.id,
                    "grain": dto.grain,
                    "entity_external_ref": dto.entity_external_ref,
                    "entity_id": entity.id if entity else False,
                    "report_date": dto.report_date,
                    "period_start_utc": dto.period_start_utc,
                    "period_end_utc": dto.period_end_utc,
                    "report_timezone": dto.report_timezone,
                    "currency": dto.currency,
                    "metric_origin": dto.metric_origin,
                    "dimension_hash": dto.dimension_hash,
                    "dimensions_json": dto.dimensions,
                    "reporting_context_hash": dto.reporting_context_hash,
                    **self._metric_values(dto),
                    "first_observed_at": dto.observed_at,
                    "last_observed_at": dto.observed_at,
                    "current_content_hash": dto.content_hash,
                    "current_revision_sequence": 0,
                    "last_sync_run_id": sync_run.id if sync_run else False,
                }
            )
        )

    @api.model
    def _create_revision(self, metric, dto, sequence, sync_run):
        return (
            self.env["marketing.center.metric.revision"]
            .sudo()
            .with_company(metric.company_id)
            .with_context(
                marketing_performance_write_token=MARKETING_PERFORMANCE_WRITE_TOKEN
            )
            .create(
                {
                    "metric_id": metric.id,
                    "revision_sequence": sequence,
                    "content_hash": dto.content_hash,
                    "schema_version": dto.schema_version,
                    "observed_at": dto.observed_at,
                    "sync_run_id": sync_run.id if sync_run else False,
                    **self._metric_values(dto),
                    "snapshot_json": dto.canonical_content(),
                }
            )
        )

    @api.model
    def _write_metric(self, metric, values):
        metric.with_context(
            marketing_performance_write_token=MARKETING_PERFORMANCE_WRITE_TOKEN
        ).write(values)

    @api.model
    def _result(self, metric, disposition):
        return PerformanceIngestResult(
            metric_id=metric.id,
            disposition=disposition,
            revision_sequence=metric.current_revision_sequence,
            content_hash=metric.current_content_hash,
        )

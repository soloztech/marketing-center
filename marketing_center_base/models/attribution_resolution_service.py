import dataclasses
import datetime
import logging

from psycopg2.errors import SerializationFailure

from odoo import api, fields, models

from ..services.serialization import acquire_advisory_xact_lock
from .attribution_resolution import ASSET_RESOLUTION_WRITE_TOKEN

_logger = logging.getLogger(__name__)

_RESOLVER_VERSION = 1
_MAX_BACKFILL_BATCH = 1000
_CATALOG_RETRY_LIMIT = 100


@dataclasses.dataclass(frozen=True)
class AssetResolverSpec:
    provider_key: str
    service_key: str
    target_kind: str
    entity_type: str = ""
    ref_segment: str = ""
    polymorphic: bool = False


class MarketingAttributionAssetResolutionService(models.AbstractModel):
    _name = "marketing.attribution.asset.resolution.service"
    _description = "Marketing Attribution Asset Resolution Service"

    @api.model
    def _asset_resolver_specs(self):
        """Provider-neutral extension point keyed by public asset namespace."""

        return {}

    @api.model
    def _resolve_canonical_key(self, company_id, canonical_key):
        if (
            not isinstance(company_id, int)
            or isinstance(company_id, bool)
            or company_id <= 0
            or not canonical_key
        ):
            return 0
        lock_key = "marketing_asset_resolution:%s:%s" % (
            company_id,
            canonical_key,
        )
        acquire_advisory_xact_lock(
            self.env.cr,
            lock_key,
            "Concurrent asset resolution requires a fresh snapshot",
        )
        company = self.env["res.company"].sudo().browse(company_id).exists()
        if len(company) != 1:
            return 0
        effective = (
            self.env["marketing.attribution.effective.touchpoint"]
            .sudo()
            .with_company(company)
            .search(
                [
                    ("company_id", "=", company.id),
                    ("canonical_key", "=", canonical_key),
                ],
                limit=1,
            )
        )
        if not effective:
            return self._remove_projection(company.id, canonical_key)
        return self.with_company(company)._project_effective_touchpoint(effective)

    @api.model
    def _project_effective_touchpoint(self, effective):
        effective.ensure_one()
        resolution_model = self._resolution_model()
        existing = resolution_model.search(
            [
                ("company_id", "=", effective.company_id.id),
                ("canonical_key", "=", effective.canonical_key),
            ]
        )
        assets = effective.asset_refs_json
        assets = assets if isinstance(assets, dict) else {}
        specs = self._asset_resolver_specs()
        now = fields.Datetime.now()
        kept_ids = []
        by_namespace = {item.asset_namespace: item for item in existing}
        for namespace in sorted(assets):
            value = self._asset_value(assets[namespace])
            values = self._resolution_values(
                effective,
                namespace,
                value,
                assets,
                specs,
            )
            record = by_namespace.get(namespace)
            if record:
                values.update(
                    {
                        "attempt_count": record.attempt_count + 1,
                        "first_attempted_at": record.first_attempted_at,
                        "resolved_at": self._resolved_at(record, values, now),
                    }
                )
                record.write(values)
            else:
                values.update(
                    {
                        "attempt_count": 1,
                        "first_attempted_at": now,
                        "resolved_at": now if values["state"] == "resolved" else False,
                    }
                )
                record = resolution_model.create(values)
            kept_ids.append(record.id)
        stale = existing.filtered(lambda item: item.id not in kept_ids)
        if stale:
            stale.unlink()
        return len(kept_ids)

    @api.model
    def _resolution_values(self, effective, namespace, value, assets, specs):
        now = fields.Datetime.now()
        spec = specs.get(namespace.lower())
        base = {
            "company_id": effective.company_id.id,
            "touchpoint_id": effective.touchpoint_id.id,
            "canonical_key": effective.canonical_key,
            "asset_namespace": namespace[:128],
            "asset_value": value,
            "resolver_version": _RESOLVER_VERSION,
            "target_kind": spec.target_kind if spec else "unknown",
            "provider_key": spec.provider_key if spec else "",
            "service_key": spec.service_key if spec else "",
            "mapped_entity_type": spec.entity_type if spec else "",
            "canonical_external_ref": "",
            "source_id": False,
            "entity_id": False,
            "state": "unsupported" if not spec else "unresolved",
            "reason": "unsupported_namespace" if not spec else "not_resolved",
            "source_candidate_count": 0,
            "entity_candidate_count": 0,
            "last_attempted_at": now,
        }
        if not spec:
            return base
        if not value:
            base["reason"] = "empty_asset_value"
            return base
        return self._resolve_asset_reference(
            base,
            effective,
            spec,
            value,
            assets,
            specs,
        )

    @api.model
    def _resolve_asset_reference(self, base, effective, spec, value, assets, specs):
        """Provider hook for a registered public namespace.

        A provider addon registers its specs through ``_asset_resolver_specs``
        and overrides this method for the specs it owns.  Falling back here is
        explicit: a namespace must never be guessed from a catalog row.
        """

        base.update(
            {
                "state": "unsupported",
                "reason": "provider_resolver_not_implemented",
            }
        )
        return base

    @api.model
    def _asset_value(self, value):
        if value in (None, False):
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.strip()[:512]

    @api.model
    def _resolved_at(self, record, values, now):
        if values["state"] != "resolved":
            return False
        return record.resolved_at or now

    @api.model
    def _resolution_model(self):
        return (
            self.env["marketing.attribution.asset.resolution"]
            .sudo()
            .with_context(
                marketing_asset_resolution_write_token=ASSET_RESOLUTION_WRITE_TOKEN
            )
        )

    @api.model
    def _remove_projection(self, company_id, canonical_key):
        records = self._resolution_model().search(
            [
                ("company_id", "=", company_id),
                ("canonical_key", "=", canonical_key),
            ]
        )
        count = len(records)
        records.unlink()
        return count

    @api.model
    def _retry_after_catalog(self, source, entity, limit=_CATALOG_RETRY_LIMIT):
        source = source.sudo().exists()
        entity = entity.sudo().exists()
        if len(source) != 1 or len(entity) != 1 or entity.source_id != source:
            return 0
        pending = self._catalog_retry_candidates(source, entity, limit)
        keys = []
        for resolution in pending:
            if resolution.company_id != source.company_id:
                continue
            key = (resolution.company_id.id, resolution.canonical_key)
            if key not in keys:
                keys.append(key)
            if len(keys) >= limit:
                break
        return self._resolve_keys_safely(keys)

    @api.model
    def _catalog_retry_candidates(self, source, entity, limit):
        """Provider hook returning resolution rows affected by a catalog upsert."""

        return self.env["marketing.attribution.asset.resolution"].browse()

    @api.model
    def _cron_backfill(self, limit=200, retry_after_minutes=5):
        limit = self._bounded_limit(limit)
        retry_after_minutes = max(0, min(int(retry_after_minutes), 24 * 60))
        missing = self._missing_projection_keys(limit)
        remaining = limit - len(missing)
        retry = self._retry_projection_keys(remaining, retry_after_minutes)
        return self._resolve_keys_safely(missing + retry)

    @api.model
    def _missing_projection_keys(self, limit):
        self.env.cr.execute(
            """
            SELECT effective.company_id, effective.canonical_key
            FROM marketing_attribution_effective_touchpoint AS effective
            WHERE (
                jsonb_typeof(effective.asset_refs_json) = 'object'
                AND effective.asset_refs_json <> '{}'::jsonb
                AND NOT EXISTS (
                    SELECT 1
                    FROM marketing_attribution_asset_resolution AS resolution
                    WHERE resolution.company_id = effective.company_id
                      AND resolution.canonical_key = effective.canonical_key
                      AND resolution.touchpoint_id = effective.touchpoint_id
                )
            ) OR EXISTS (
                SELECT 1
                FROM marketing_attribution_asset_resolution AS stale
                WHERE stale.company_id = effective.company_id
                  AND stale.canonical_key = effective.canonical_key
                  AND stale.touchpoint_id <> effective.touchpoint_id
            )
            ORDER BY effective.id
            LIMIT %s
            """,
            [limit],
        )
        return [tuple(row) for row in self.env.cr.fetchall()]

    @api.model
    def _retry_projection_keys(self, limit, retry_after_minutes):
        if limit <= 0:
            return []
        cutoff = fields.Datetime.now() - datetime.timedelta(minutes=retry_after_minutes)
        self.env.cr.execute(
            """
            SELECT company_id, canonical_key
            FROM marketing_attribution_asset_resolution
            WHERE (
                state IN ('unresolved', 'ambiguous')
                AND last_attempted_at <= %s
            ) OR resolver_version < %s
            GROUP BY company_id, canonical_key
            ORDER BY MIN(last_attempted_at), company_id, canonical_key
            LIMIT %s
            """,
            [cutoff, _RESOLVER_VERSION, limit],
        )
        return [tuple(row) for row in self.env.cr.fetchall()]

    @api.model
    def _resolve_keys_safely(self, keys):
        processed = 0
        for company_id, canonical_key in keys:
            try:
                with self.env.cr.savepoint():
                    self._resolve_canonical_key(company_id, canonical_key)
                processed += 1
            except SerializationFailure:
                raise
            except Exception:  # pragma: no cover - operational isolation guard
                _logger.exception(
                    "Asset resolution failed for company %s / touchpoint %s",
                    company_id,
                    canonical_key,
                )
        return processed

    @api.model
    def _bounded_limit(self, limit):
        if not isinstance(limit, int) or isinstance(limit, bool):
            return 200
        return max(1, min(limit, _MAX_BACKFILL_BATCH))


class MarketingAttributionServiceAssetResolution(models.AbstractModel):
    _inherit = "marketing.attribution.service"

    @api.model
    def _ingest_touchpoint(self, company, payload):
        result = super()._ingest_touchpoint(company, payload)
        service = self.env["marketing.attribution.asset.resolution.service"].sudo()
        service._resolve_keys_safely([(company.id, result.canonical_key)])
        return result


class MarketingCenterCatalogServiceAssetResolution(models.AbstractModel):
    _inherit = "marketing.center.catalog.service"

    @api.model
    def _upsert_entity(self, company, source, payload, sync_run=None):
        result = super()._upsert_entity(company, source, payload, sync_run=sync_run)
        entity = (
            self.env["marketing.center.external.entity"].sudo().browse(result.entity_id)
        )
        service = self.env["marketing.attribution.asset.resolution.service"].sudo()
        try:
            with self.env.cr.savepoint():
                service._retry_after_catalog(source, entity)
        except SerializationFailure:
            raise
        except Exception:  # pragma: no cover - catalog ingestion must remain durable
            _logger.exception(
                "Asset-resolution retry failed after catalog entity %s", entity.id
            )
        return result

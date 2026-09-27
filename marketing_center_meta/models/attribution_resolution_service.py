import dataclasses
import re

from odoo import api, models

from odoo.addons.marketing_center_base.models.attribution_resolution_service import (
    AssetResolverSpec,
)

from ..services import catalog as meta_catalog

_META_PROVIDER_KEY = "meta"
_META_SERVICE_KEY = "meta.ads"

_META_ACCOUNT_RE = re.compile(r"^act_([0-9]+)$")
_META_OBJECT_RE = re.compile(r"^[0-9]+$")
_META_CANONICAL_REF_RE = re.compile(
    r"^(act_[0-9]+)/(campaigns|adsets|ads|creatives|forms)/([0-9]+)$"
)

_META_SPECS = {
    "meta.ad_account_id": AssetResolverSpec(
        _META_PROVIDER_KEY, _META_SERVICE_KEY, "source"
    ),
    "meta.account_id": AssetResolverSpec(
        _META_PROVIDER_KEY, _META_SERVICE_KEY, "source"
    ),
    "meta.ad_account_ref": AssetResolverSpec(
        _META_PROVIDER_KEY, _META_SERVICE_KEY, "source"
    ),
    "meta.campaign_id": AssetResolverSpec(
        _META_PROVIDER_KEY,
        _META_SERVICE_KEY,
        "entity",
        "campaign",
        "campaigns",
    ),
    "meta.ad_campaign_id": AssetResolverSpec(
        _META_PROVIDER_KEY,
        _META_SERVICE_KEY,
        "entity",
        "campaign",
        "campaigns",
    ),
    "meta.adset_id": AssetResolverSpec(
        _META_PROVIDER_KEY, _META_SERVICE_KEY, "entity", "group", "adsets"
    ),
    "meta.ad_set_id": AssetResolverSpec(
        _META_PROVIDER_KEY, _META_SERVICE_KEY, "entity", "group", "adsets"
    ),
    "meta.ad_id": AssetResolverSpec(
        _META_PROVIDER_KEY, _META_SERVICE_KEY, "entity", "ad", "ads"
    ),
    "meta.creative_id": AssetResolverSpec(
        _META_PROVIDER_KEY,
        _META_SERVICE_KEY,
        "entity",
        "creative",
        "creatives",
    ),
    "meta.ad_creative_id": AssetResolverSpec(
        _META_PROVIDER_KEY,
        _META_SERVICE_KEY,
        "entity",
        "creative",
        "creatives",
    ),
    "meta.form_id": AssetResolverSpec(
        _META_PROVIDER_KEY, _META_SERVICE_KEY, "entity", "form", "forms"
    ),
    "meta.leadgen_form_id": AssetResolverSpec(
        _META_PROVIDER_KEY, _META_SERVICE_KEY, "entity", "form", "forms"
    ),
    # externalAdReply.sourceID is intentionally not assumed to be an ad. It is
    # resolved only when the account catalog has one unique matching entity.
    "meta.source_id": AssetResolverSpec(
        _META_PROVIDER_KEY,
        _META_SERVICE_KEY,
        "entity",
        polymorphic=True,
    ),
}

_META_SEGMENT_TYPES = {
    "campaigns": "campaign",
    "adsets": "group",
    "ads": "ad",
    "creatives": "creative",
    "forms": "form",
}
_UNCATALOGUED_REASON = "catalog_entity_type_not_synchronized"
_REACTIVATION_LIMIT = 200


class MarketingAttributionAssetResolutionServiceMeta(models.AbstractModel):
    _inherit = "marketing.attribution.asset.resolution.service"

    @api.model
    def _asset_resolver_specs(self):
        specs = dict(super()._asset_resolver_specs())
        specs.update(_META_SPECS)
        return specs

    @api.model
    def _resolve_asset_reference(self, base, effective, spec, value, assets, specs):
        if spec.provider_key != _META_PROVIDER_KEY:
            return super()._resolve_asset_reference(
                base,
                effective,
                spec,
                value,
                assets,
                specs,
            )
        uncatalogued = self._meta_uncatalogued_entity_type(spec, value)
        if uncatalogued:
            # No Meta sync produces this entity type, so a retry can never
            # resolve it; keep it out of the unresolved retry queue.
            base.update(
                {
                    "state": "unsupported",
                    "reason": _UNCATALOGUED_REASON,
                    "mapped_entity_type": uncatalogued,
                }
            )
            return base
        account_hints = self._meta_account_hints(
            assets,
            specs,
            spec.service_key,
        )
        if len(account_hints) > 1:
            sources = self._meta_source_candidates(
                effective.company_id,
                spec.service_key,
                account_hints,
            )
            base.update(
                {
                    "state": "ambiguous",
                    "reason": "conflicting_account_hints",
                    "source_candidate_count": len(sources),
                }
            )
            return base
        if spec.target_kind == "source":
            return self._meta_resolve_source(base, effective, spec, value)
        if spec.polymorphic:
            return self._meta_resolve_polymorphic(
                base,
                effective,
                spec,
                value,
                account_hints,
            )
        return self._meta_resolve_typed_entity(
            base,
            effective,
            spec,
            value,
            account_hints,
        )

    @api.model
    def _meta_uncatalogued_entity_type(self, spec, value):
        """Return a concrete entity type that the Meta catalog never produces."""

        entity_type = spec.entity_type
        if spec.polymorphic:
            canonical = _META_CANONICAL_REF_RE.fullmatch(value)
            entity_type = _META_SEGMENT_TYPES[canonical.group(2)] if canonical else ""
        if entity_type and entity_type not in meta_catalog.META_CATALOG_ENTITY_TYPES:
            return entity_type
        return ""

    @api.model
    def _reactivate_uncatalogued_forms(
        self, company, limit=_REACTIVATION_LIMIT, dry_run=True
    ):
        """Re-project form references parked as unsupported by this resolver.

        Use once a Meta sync starts producing form entities (a rollback of this
        code needs the self-contained procedure in the operations guide, since
        this helper is removed with it). The selector does not depend on the
        namespace spelling, so mixed-case aliases and canonical ``forms``
        references in ``meta.source_id`` are included. ``dry_run`` only
        returns the bounded, de-duplicated keys.
        """

        company = company.sudo().ensure_one()
        limit = max(1, min(int(limit), _REACTIVATION_LIMIT))
        rows = (
            self.env["marketing.attribution.asset.resolution"]
            .sudo()
            .search(
                [
                    ("company_id", "=", company.id),
                    ("provider_key", "=", _META_PROVIDER_KEY),
                    ("service_key", "=", _META_SERVICE_KEY),
                    ("mapped_entity_type", "=", "form"),
                    ("state", "=", "unsupported"),
                    ("reason", "=", _UNCATALOGUED_REASON),
                ],
                order="id",
            )
        )
        keys = []
        for row in rows:
            key = (row.company_id.id, row.canonical_key)
            if key not in keys:
                keys.append(key)
            if len(keys) >= limit:
                break
        if dry_run:
            return keys
        return self._resolve_keys_safely(keys)

    @api.model
    def _meta_resolve_source(self, base, effective, spec, value):
        account_ref = self._meta_account_ref(value)
        if not account_ref:
            base["reason"] = "invalid_account_reference"
            return base
        sources = self._meta_source_candidates(
            effective.company_id,
            spec.service_key,
            {account_ref},
        )
        base.update(
            {
                "canonical_external_ref": account_ref,
                "source_candidate_count": len(sources),
            }
        )
        if len(sources) == 1:
            base.update(
                {
                    "source_id": sources.id,
                    "canonical_external_ref": sources.external_account_ref,
                    "state": "resolved",
                    "reason": "source_resolved",
                }
            )
        elif len(sources) > 1:
            base.update({"state": "ambiguous", "reason": "multiple_sources"})
        else:
            base["reason"] = "source_not_found"
        return base

    @api.model
    def _meta_resolve_typed_entity(
        self,
        base,
        effective,
        spec,
        value,
        account_hints,
    ):
        parsed = self._meta_object_identity(value, spec.ref_segment)
        if not parsed:
            base["reason"] = "invalid_entity_reference"
            return base
        embedded_account, object_id = parsed
        hints = set(account_hints)
        if embedded_account:
            hints.add(embedded_account)
        if len(hints) > 1:
            base.update({"state": "ambiguous", "reason": "conflicting_account_hints"})
            return base
        sources = self._meta_source_candidates(
            effective.company_id,
            spec.service_key,
            hints,
        )
        base["source_candidate_count"] = len(sources)
        if hints and not sources:
            base["reason"] = "source_not_found"
            return base
        if hints and len(sources) > 1:
            base.update({"state": "ambiguous", "reason": "multiple_sources"})
            return base
        entities = self._meta_typed_entity_candidates(
            effective.company_id,
            spec,
            object_id,
            sources if hints else self.env["marketing.center.source"],
        )
        base["entity_candidate_count"] = len(entities)
        if len(entities) == 1:
            entity = entities
            base.update(
                {
                    "source_id": entity.source_id.id,
                    "entity_id": entity.id,
                    "canonical_external_ref": entity.external_ref,
                    "state": "resolved",
                    "reason": "entity_resolved",
                    "source_candidate_count": 1,
                }
            )
            return base
        if len(entities) > 1:
            base.update({"state": "ambiguous", "reason": "multiple_entities"})
            return base
        if len(sources) == 1:
            base.update(
                {
                    "source_id": sources.id,
                    "canonical_external_ref": "%s/%s/%s"
                    % (sources.external_account_ref, spec.ref_segment, object_id),
                    "reason": "entity_not_in_catalog",
                }
            )
        elif len(sources) > 1:
            base.update({"state": "ambiguous", "reason": "multiple_sources"})
        elif not sources:
            base["reason"] = "source_not_found"
        return base

    @api.model
    def _meta_resolve_polymorphic(
        self,
        base,
        effective,
        spec,
        value,
        account_hints,
    ):
        canonical = _META_CANONICAL_REF_RE.fullmatch(value)
        if canonical:
            segment = canonical.group(2)
            dynamic_spec = dataclasses.replace(
                spec,
                entity_type=_META_SEGMENT_TYPES[segment],
                ref_segment=segment,
                polymorphic=False,
            )
            base["mapped_entity_type"] = dynamic_spec.entity_type
            return self._meta_resolve_typed_entity(
                base,
                effective,
                dynamic_spec,
                value,
                account_hints,
            )
        if not _META_OBJECT_RE.fullmatch(value):
            base["reason"] = "invalid_entity_reference"
            return base
        sources = self._meta_source_candidates(
            effective.company_id,
            spec.service_key,
            account_hints,
        )
        base["source_candidate_count"] = len(sources)
        if account_hints and not sources:
            base["reason"] = "source_not_found"
            return base
        entities = self._meta_polymorphic_entity_candidates(
            effective.company_id,
            spec.service_key,
            value,
            sources if account_hints else self.env["marketing.center.source"],
        )
        base["entity_candidate_count"] = len(entities)
        if len(entities) == 1:
            entity = entities
            base.update(
                {
                    "source_id": entity.source_id.id,
                    "entity_id": entity.id,
                    "mapped_entity_type": entity.entity_type,
                    "canonical_external_ref": entity.external_ref,
                    "state": "resolved",
                    "reason": "unique_polymorphic_entity",
                    "source_candidate_count": 1,
                }
            )
        elif len(entities) > 1:
            base.update({"state": "ambiguous", "reason": "multiple_entity_types"})
        elif len(sources) == 1:
            base.update(
                {
                    "source_id": sources.id,
                    "reason": "entity_not_in_catalog",
                }
            )
        elif len(sources) > 1:
            base.update({"state": "ambiguous", "reason": "multiple_sources"})
        else:
            base["reason"] = "source_not_found"
        return base

    @api.model
    def _meta_typed_entity_candidates(self, company, spec, object_id, sources):
        entity_model = (
            self.env["marketing.center.external.entity"]
            .sudo()
            .with_context(active_test=False)
        )
        domain = [
            ("company_id", "=", company.id),
            ("source_id.service", "=", spec.service_key),
            ("entity_type", "=", spec.entity_type),
        ]
        if sources:
            domain.append(("source_id", "in", sources.ids))
        if len(sources) == 1:
            canonical_ref = "%s/%s/%s" % (
                sources.external_account_ref,
                spec.ref_segment,
                object_id,
            )
            exact = entity_model.search(domain + [("external_ref", "=", canonical_ref)])
            if exact:
                return exact
        return entity_model.search(domain + [("external_id", "=", object_id)], limit=3)

    @api.model
    def _meta_polymorphic_entity_candidates(
        self,
        company,
        service_key,
        object_id,
        sources,
    ):
        domain = [
            ("company_id", "=", company.id),
            ("source_id.service", "=", service_key),
            ("entity_type", "in", list(_META_SEGMENT_TYPES.values())),
            ("external_id", "=", object_id),
        ]
        if sources:
            domain.append(("source_id", "in", sources.ids))
        return (
            self.env["marketing.center.external.entity"]
            .sudo()
            .with_context(active_test=False)
            .search(domain, limit=3)
        )

    @api.model
    def _meta_source_candidates(self, company, service_key, account_hints):
        domain = [
            ("company_id", "=", company.id),
            ("service", "=", service_key),
        ]
        if account_hints:
            account_refs = sorted(account_hints)
            account_ids = [item[4:] for item in account_refs]
            domain.extend(
                [
                    "|",
                    ("external_account_ref", "in", account_refs),
                    ("external_account_id", "in", account_ids + account_refs),
                ]
            )
        return (
            self.env["marketing.center.source"]
            .sudo()
            .with_context(active_test=False)
            .search(domain, limit=3)
        )

    @api.model
    def _meta_account_hints(self, assets, specs, service_key):
        hints = set()
        for namespace, raw_value in assets.items():
            spec = specs.get(str(namespace).lower())
            if not spec or spec.service_key != service_key:
                continue
            value = self._asset_value(raw_value)
            if spec.target_kind == "source":
                account_ref = self._meta_account_ref(value)
                if account_ref:
                    hints.add(account_ref)
                continue
            canonical = _META_CANONICAL_REF_RE.fullmatch(value)
            if canonical:
                hints.add(canonical.group(1))
        return hints

    @api.model
    def _meta_account_ref(self, value):
        value = self._asset_value(value)
        if _META_OBJECT_RE.fullmatch(value):
            return "act_%s" % value
        match = _META_ACCOUNT_RE.fullmatch(value)
        return "act_%s" % match.group(1) if match else ""

    @api.model
    def _meta_object_identity(self, value, expected_segment):
        value = self._asset_value(value)
        if _META_OBJECT_RE.fullmatch(value):
            return "", value
        match = _META_CANONICAL_REF_RE.fullmatch(value)
        if not match or match.group(2) != expected_segment:
            return None
        return match.group(1), match.group(3)

    @api.model
    def _catalog_retry_candidates(self, source, entity, limit):
        candidates = super()._catalog_retry_candidates(source, entity, limit)
        if source.service != _META_SERVICE_KEY:
            return candidates
        resolution_model = self.env["marketing.attribution.asset.resolution"].sudo()
        pending = resolution_model.search(
            [
                ("company_id", "=", source.company_id.id),
                ("provider_key", "=", _META_PROVIDER_KEY),
                ("service_key", "=", _META_SERVICE_KEY),
                ("state", "in", ["unresolved", "ambiguous"]),
                "|",
                ("source_id", "=", source.id),
                ("source_id", "=", False),
            ],
            order="last_attempted_at asc, id asc",
            limit=limit,
        )
        polymorphic = resolution_model.search(
            [
                ("company_id", "=", source.company_id.id),
                ("provider_key", "=", _META_PROVIDER_KEY),
                ("service_key", "=", _META_SERVICE_KEY),
                ("asset_namespace", "=ilike", "meta.source_id"),
                ("asset_value", "in", [entity.external_id, entity.external_ref]),
            ],
            order="last_attempted_at asc, id asc",
            limit=limit,
        )
        return candidates | pending | polymorphic

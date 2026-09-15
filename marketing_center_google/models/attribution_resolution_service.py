import re

from odoo import api, models

from odoo.addons.marketing_center_base.models.attribution_resolution_service import (
    AssetResolverSpec,
)

_SPECS = {
    "google.customer_id": AssetResolverSpec("google", "google.ads", "source"),
    "google.campaign_id": AssetResolverSpec(
        "google", "google.ads", "entity", "campaign", "campaigns"
    ),
    "google.ad_group_id": AssetResolverSpec(
        "google", "google.ads", "entity", "ad_group", "adGroups"
    ),
    "google.ad_id": AssetResolverSpec(
        "google", "google.ads", "entity", "ad", "adGroupAds"
    ),
}
_REF = re.compile(
    r"^customers/([0-9]{10})/(campaigns|adGroups|adGroupAds)/([0-9]+(?:~[0-9]+)?)$"
)


class MarketingAssetResolutionGoogle(models.AbstractModel):
    _inherit = "marketing.attribution.asset.resolution.service"

    @api.model
    def _asset_resolver_specs(self):
        return {**super()._asset_resolver_specs(), **_SPECS}

    @api.model
    def _resolve_asset_reference(self, base, effective, spec, value, assets, specs):
        if spec.provider_key != "google":
            return super()._resolve_asset_reference(
                base, effective, spec, value, assets, specs
            )
        customer = str(assets.get("google.customer_id") or "")
        hints = {customer} if re.fullmatch(r"[0-9]{10}", customer) else set()
        for namespace in _SPECS:
            candidate = _REF.fullmatch(str(assets.get(namespace) or ""))
            if candidate:
                hints.add(candidate[1])
        if len(hints) != 1:
            base.update(
                state="ambiguous" if len(hints) > 1 else "unresolved",
                reason="conflicting_account_hints" if hints else "account_required",
            )
            return base
        customer = next(iter(hints))
        source = (
            self.env["marketing.center.source"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("company_id", "=", effective.company_id.id),
                    ("service", "=", "google.ads"),
                    ("external_account_ref", "=", "customers/%s" % customer),
                ],
                limit=3,
            )
        )
        base["source_candidate_count"] = len(source)
        if len(source) != 1:
            base.update(
                state="ambiguous" if source else "unresolved",
                reason="multiple_sources" if source else "source_not_found",
            )
            return base
        base["source_id"] = source.id
        if spec.target_kind == "source":
            if value != customer:
                base.update(reason="invalid_account_reference")
                return base
            base.update(
                state="resolved",
                reason="source_resolved",
                canonical_external_ref=source.external_account_ref,
            )
            return base
        match = _REF.fullmatch(value)
        if not match or match[1] != customer or match[2] != spec.ref_segment:
            base["reason"] = "invalid_entity_reference"
            return base
        base["canonical_external_ref"] = value
        entity = (
            self.env["marketing.center.external.entity"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("source_id", "=", source.id),
                    ("company_id", "=", effective.company_id.id),
                    ("entity_type", "=", spec.entity_type),
                    ("external_ref", "=", value),
                ],
                limit=3,
            )
        )
        base["entity_candidate_count"] = len(entity)
        if len(entity) == 1:
            base.update(state="resolved", reason="entity_resolved", entity_id=entity.id)
        else:
            base.update(
                state="ambiguous" if entity else "unresolved",
                reason="multiple_entities" if entity else "entity_not_found",
            )
        return base

    @api.model
    def _catalog_retry_candidates(self, source, entity, limit):
        result = super()._catalog_retry_candidates(source, entity, limit)
        if source.service != "google.ads":
            return result
        return result | self.env[
            "marketing.attribution.asset.resolution"
        ].sudo().search(
            [
                ("company_id", "=", source.company_id.id),
                ("service_key", "=", "google.ads"),
                ("state", "in", ["unresolved", "ambiguous"]),
                ("asset_value", "=", entity.external_ref),
                "|",
                ("source_id", "=", source.id),
                ("source_id", "=", False),
            ],
            limit=limit,
        )

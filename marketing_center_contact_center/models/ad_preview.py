"""Enrich only an exactly linked, current, unambiguous catalog ad."""

import hashlib

from odoo import api, models

from ..services.mapper import MAPPING_VERSION, ContactCenterAttributionMapper


class AuthorizedMarketingPreview(dict):
    """Private authorization data stays in memory and outside serialized copy."""

    def __init__(self, values, entity, fingerprint, source_hash):
        super().__init__(values)
        self.entity_id = entity.id
        self.fingerprint = fingerprint
        self.source_hash = source_hash


class ContactCenterAttributionPreview(models.Model):
    _inherit = "contact.center.attribution.preview"

    def _marketing_preview_entity(self):
        self.ensure_one()
        empty = self.env["marketing.center.external.entity"]
        touchpoint = self.touchpoint_id
        if (
            not touchpoint
            or self.company_id not in self.env.companies
            or touchpoint.company_id != self.company_id
            or touchpoint.account_id != self.account_id
            or not self.account_id.ad_preview_enrichment_enabled
        ):
            return empty
        dto = ContactCenterAttributionMapper.to_dto(touchpoint)
        links = (
            self.env["marketing.attribution.contact.center.link"]
            .sudo()
            .search(
                [
                    ("company_id", "=", self.company_id.id),
                    ("source_touchpoint_id", "=", touchpoint.id),
                    ("source_public_ref", "=", touchpoint.public_ref),
                    ("source_content_hash", "=", dto.content_hash),
                    ("mapping_version", "=", MAPPING_VERSION),
                ],
                limit=2,
            )
        )
        if len(links) != 1:
            return empty
        target = links.marketing_touchpoint_id
        effective = (
            self.env["marketing.attribution.effective.touchpoint"]
            .sudo()
            .search(
                [
                    ("company_id", "=", self.company_id.id),
                    ("canonical_key", "=", dto.canonical_key),
                ],
                limit=2,
            )
        )
        if len(effective) != 1 or effective.touchpoint_id != target:
            return empty
        resolutions = (
            self.env["marketing.attribution.asset.resolution"]
            .sudo()
            .search(
                [
                    ("company_id", "=", self.company_id.id),
                    ("touchpoint_id", "=", target.id),
                    ("canonical_key", "=", dto.canonical_key),
                    ("provider_key", "=", "meta"),
                    ("service_key", "=", "meta.ads"),
                ],
                limit=65,
            )
        )
        if len(resolutions) > 64 or any(
            item.state == "ambiguous" for item in resolutions
        ):
            return empty
        relevant = resolutions.filtered(
            lambda item: item.asset_namespace.lower()
            in {"meta.ad_id", "meta.source_id"}
        )
        if not relevant or any(item.state != "resolved" for item in relevant):
            return empty
        entities = relevant.mapped("entity_id")
        if len(entities) != 1 or entities.entity_type != "ad":
            return empty
        sources = resolutions.filtered(lambda item: item.state == "resolved").mapped(
            "source_id"
        )
        if (
            len(sources) != 1
            or entities.source_id != sources
            or entities.company_id != self.company_id
            or sources.company_id != self.company_id
        ):
            return empty
        return entities

    def _marketing_preview_values(self):
        base = super()._marketing_preview_values()
        service_name = "marketing.center.meta.ad.preview.service"
        if service_name not in self.env:
            return base
        entity = self._marketing_preview_entity()
        if not entity:
            return base
        source_hash = ContactCenterAttributionMapper.to_dto(
            self.touchpoint_id
        ).content_hash
        values = self.env[service_name]._fetch_ad_preview(entity)
        for record in (self.account_id, self.touchpoint_id):
            record.invalidate_recordset()
        if not values or self._marketing_preview_entity() != entity:
            return base
        return AuthorizedMarketingPreview(
            dict(values, **base),
            entity,
            getattr(values, "fingerprint", None),
            source_hash,
        )

    def _marketing_preview_validate(self, values):
        if not super()._marketing_preview_validate(values):
            return False
        if not isinstance(values, AuthorizedMarketingPreview):
            return True
        # No locks survive across either Graph or thumbnail HTTP. NOWAIT avoids
        # waiting while holding authorization locks in a different lock order.
        for record in (self.account_id, self.touchpoint_id):
            self.env.cr.execute(
                'SELECT id FROM "%s" WHERE id = %%s FOR SHARE NOWAIT' % record._table,
                [record.id],
            )
            if not self.env.cr.fetchone():
                return False
            record.invalidate_recordset()
        entity = self._marketing_preview_entity()
        if (
            not entity
            or entity.id != values.entity_id
            or ContactCenterAttributionMapper.to_dto(self.touchpoint_id).content_hash
            != values.source_hash
        ):
            return False
        return self.env[
            "marketing.center.meta.ad.preview.service"
        ]._validate_ad_preview(entity, values.fingerprint)

    def _wake_from_marketing(self):
        """One wake per exact useful attribution/catalog revision, without I/O."""
        for preview in self:
            entity = preview._marketing_preview_entity()
            if not entity:
                continue
            source_hash = ContactCenterAttributionMapper.to_dto(
                preview.touchpoint_id
            ).content_hash
            wake_key = hashlib.sha256(
                (
                    "%s:%s:%s" % (source_hash, entity.id, entity.current_content_hash)
                ).encode()
            ).hexdigest()
            preview._wake_marketing_enrichment(wake_key)
        return True


class MarketingContactCenterAttributionService(models.AbstractModel):
    _inherit = "marketing.contact.center.attribution.service"

    @api.model
    def _sync_touchpoint(self, source):
        link = super()._sync_touchpoint(source)
        self.env["contact.center.attribution.preview"].sudo().search(
            [("touchpoint_id", "=", source.id), ("expired", "=", False)], limit=1
        )._wake_from_marketing()
        return link


class MarketingAttributionResolutionService(models.AbstractModel):
    _inherit = "marketing.attribution.asset.resolution.service"

    @api.model
    def _resolve_canonical_key(self, company_id, canonical_key):
        result = super()._resolve_canonical_key(company_id, canonical_key)
        links = (
            self.env["marketing.attribution.contact.center.link"]
            .sudo()
            .search(
                [
                    ("company_id", "=", company_id),
                    ("marketing_touchpoint_id.canonical_key", "=", canonical_key),
                ],
                limit=100,
            )
        )
        self.env["contact.center.attribution.preview"].sudo().search(
            [
                ("touchpoint_id", "in", links.source_touchpoint_id.ids),
                ("expired", "=", False),
            ]
        )._wake_from_marketing()
        return result

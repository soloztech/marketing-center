"""Enrich only an exactly linked, current, unambiguous catalog ad."""

from odoo import models

from ..services.mapper import MAPPING_VERSION, ContactCenterAttributionMapper


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
        values = self.env[service_name]._fetch_ad_preview(entity)
        # A cache refresh alone retains the original REPEATABLE READ snapshot.
        # Post-I/O locks reject concurrent policy/evidence changes instead of
        # accepting a response with authority that has already been revoked.
        for record in (self.account_id, self.touchpoint_id):
            self.env.cr.execute(
                'SELECT id FROM "%s" WHERE id = %%s FOR SHARE' % record._table,
                [record.id],
            )
            if not self.env.cr.fetchone():
                return base
            record.invalidate_recordset()
        if self._marketing_preview_entity() != entity:
            return base
        return dict(values, **base)

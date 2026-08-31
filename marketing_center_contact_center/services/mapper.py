import hashlib

from odoo.addons.marketing_center_base.services.dto import (
    AttributionDTOValidationError,
    MarketingIdentifierDTO,
    MarketingTouchpointDTO,
    sanitize_url,
)

MAPPING_VERSION = 1
_PUBLIC_ASSET_NAMESPACES = {
    "meta.ad_id": "meta.ad_id",
    "meta.source_id": "meta.source_id",
}


def _opaque_source_occurrence(source):
    digest = hashlib.sha256(source.source_external_key.encode("utf-8")).hexdigest()
    return "%s:%s" % (source.source_key_kind, digest)


def _masked(value):
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return "%s...%s" % (value[:2], value[-4:])


def _safe_url(value):
    try:
        return sanitize_url(value, "contact_center.source_url")
    except AttributionDTOValidationError:
        return ""


class ContactCenterAttributionMapper:
    """Pure mapper from persisted Contact Center evidence to Marketing DTO v1."""

    @classmethod
    def to_dto(cls, source):
        source.ensure_one()
        identifiers = tuple(
            MarketingIdentifierDTO(
                namespace=item.namespace,
                role=item.role,
                comparison_hash=item.comparison_hash,
                masked_value=_masked(item.value),
                value_ref="contact_center:%s:%s:%s:%s"
                % (
                    source.public_ref,
                    item.namespace,
                    item.role,
                    item.comparison_hash,
                ),
                source_field=item.source_field or "",
            )
            for item in source.identifier_ids.sorted(
                key=lambda record: (
                    record.namespace,
                    record.role,
                    record.comparison_hash,
                )
            )
        )
        asset_refs = {
            _PUBLIC_ASSET_NAMESPACES[item.namespace]: item.value
            for item in source.identifier_ids
            if item.namespace in _PUBLIC_ASSET_NAMESPACES and item.value
        }
        source_url = _safe_url(source.source_url)
        extensions = {
            "contact_center.conflict_state": source.conflict_state,
            "contact_center.enrichment_state": source.enrichment_state,
            "contact_center.source_type": source.source_type or "",
            "contact_center.entry_point_source": source.entry_point_source or "",
            "contact_center.entry_point_app": source.entry_point_app or "",
            "contact_center.conversion_source": source.conversion_source or "",
            "contact_center.creative_media_type": source.creative_media_type or "",
            "contact_center.source_url_rejected": bool(
                source.source_url and not source_url
            ),
        }
        return MarketingTouchpointDTO(
            source_system="contact_center",
            source_scope_ref=source.provider_connection_id.external_ref,
            source_occurrence_ref=_opaque_source_occurrence(source),
            source_evidence_ref=source.public_ref,
            source_schema_version=source.inbox_event_id.provider_schema_version or "",
            occurred_at=source.occurred_at,
            observed_at=source.captured_at,
            platform=source.source_platform or source.account_id.platform,
            channel=source.account_id.platform,
            network=source.network,
            touchpoint_type=source.touchpoint_type,
            evidence_level=source.evidence_level,
            landing_url=source_url,
            utm={
                key: value
                for key, value in {
                    "source": source.utm_source,
                    "medium": source.utm_medium,
                    "campaign": source.utm_campaign,
                    "content": source.utm_content,
                    "term": source.utm_term,
                }.items()
                if value
            },
            asset_refs=asset_refs,
            identifiers=identifiers,
            extensions=extensions,
            mapping_version=MAPPING_VERSION,
        )

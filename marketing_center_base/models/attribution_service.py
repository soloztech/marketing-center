from odoo import _, api, models
from odoo.exceptions import AccessError, ValidationError

from ..services.dto import (
    AttributionDTOValidationError,
    AttributionIngestResult,
    MarketingTouchpointDTO,
    canonical_json,
    sha256_text,
)
from ..services.tokens import MARKETING_ATTRIBUTION_WRITE_TOKEN


class MarketingAttributionService(models.AbstractModel):
    _name = "marketing.attribution.service"
    _description = "Marketing Attribution Ingestion Service"

    @api.model
    def _ingest_touchpoint(self, company, payload):
        if (
            not company
            or getattr(company, "_name", "") != "res.company"
            or len(company) != 1
        ):
            raise ValidationError(_("A single valid company is required."))
        company = company.exists()
        if not company:
            raise ValidationError(_("A single valid company is required."))
        if company not in self.env.companies:
            raise AccessError(_("The marketing touchpoint belongs to another company."))
        try:
            dto = (
                payload
                if isinstance(payload, MarketingTouchpointDTO)
                else MarketingTouchpointDTO.from_dict(payload)
            )
        except AttributionDTOValidationError as error:
            raise ValidationError(
                _("Invalid marketing touchpoint: %s") % error
            ) from error

        canonical_key = dto.canonical_key
        content_hash = dto.content_hash
        lock_key = "marketing_attribution:%s:%s" % (company.id, canonical_key)
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [lock_key]
        )

        touchpoint_model = self.env["marketing.attribution.touchpoint"].sudo()
        existing = touchpoint_model.search(
            [
                ("company_id", "=", company.id),
                ("canonical_key", "=", canonical_key),
            ],
            order="revision_sequence asc, id asc",
        )
        exact = existing.filtered(lambda item: item.content_hash == content_hash)[:1]
        if exact:
            self._create_evidence(exact, dto, "duplicate", exact)
            return self._result(exact, "duplicate")

        previous = existing[-1:] if existing else touchpoint_model.browse()
        revision_sequence = (previous.revision_sequence or 0) + 1
        values = self._touchpoint_values(
            company, dto, canonical_key, content_hash, revision_sequence
        )
        touchpoint = (
            touchpoint_model.with_company(company)
            .with_context(
                marketing_attribution_write_token=MARKETING_ATTRIBUTION_WRITE_TOKEN
            )
            .create(values)
        )
        self._create_identifiers(touchpoint, dto)
        disposition = "conflict" if previous else "accepted"
        self._create_evidence(touchpoint, dto, disposition, previous)
        return self._result(touchpoint, disposition)

    @api.model
    def _touchpoint_values(
        self, company, dto, canonical_key, content_hash, revision_sequence
    ):
        privacy = dto.privacy
        utm = dto.utm
        return {
            "company_id": company.id,
            "schema_version": dto.schema_version,
            "mapping_version": dto.mapping_version,
            "source_system": dto.source_system,
            "source_scope_ref": dto.source_scope_ref,
            "source_occurrence_ref": dto.source_occurrence_ref,
            "source_evidence_ref": dto.source_evidence_ref or dto.source_occurrence_ref,
            "source_schema_version": dto.source_schema_version or False,
            "occurred_at": dto.occurred_at,
            "observed_at": dto.observed_at,
            "platform": dto.platform,
            "channel": dto.channel,
            "network": dto.network or False,
            "touchpoint_type": dto.touchpoint_type,
            "evidence_level": dto.evidence_level,
            "landing_url": dto.landing_url or False,
            "referrer_url": dto.referrer_url or False,
            "utm_source": utm.get("source") or False,
            "utm_medium": utm.get("medium") or False,
            "utm_campaign": utm.get("campaign") or False,
            "utm_content": utm.get("content") or False,
            "utm_term": utm.get("term") or False,
            "asset_refs_json": dto.asset_refs,
            "policy_version": privacy.policy_version or False,
            "notice_version": privacy.notice_version or False,
            "legal_basis_code": privacy.legal_basis_code or False,
            "consent_state": privacy.consent_state,
            "privacy_decision_source": privacy.decision_source or False,
            "privacy_decided_at": privacy.decided_at or False,
            "extensions_json": dto.extensions,
            "canonical_key": canonical_key,
            "content_hash": content_hash,
            "revision_sequence": revision_sequence,
        }

    @api.model
    def _create_identifiers(self, touchpoint, dto):
        if not dto.identifiers:
            return self.env["marketing.attribution.identifier"]
        values = [
            {
                "touchpoint_id": touchpoint.id,
                "namespace": item.namespace,
                "role": item.role,
                "comparison_hash": item.comparison_hash,
                "masked_value": item.masked_value or False,
                "value_ref": item.value_ref or False,
                "source_field": item.source_field or False,
                "purpose": item.purpose,
                "observed_at": dto.observed_at,
                "retain_until": item.retain_until or False,
            }
            for item in dto.identifiers
        ]
        return (
            self.env["marketing.attribution.identifier"]
            .sudo()
            .with_company(touchpoint.company_id)
            .with_context(
                marketing_attribution_write_token=MARKETING_ATTRIBUTION_WRITE_TOKEN
            )
            .create(values)
        )

    @api.model
    def _create_evidence(self, touchpoint, dto, disposition, previous):
        source_evidence_ref = dto.source_evidence_ref or dto.source_occurrence_ref
        evidence_digest = sha256_text(
            canonical_json(
                {
                    "source_system": dto.source_system,
                    "source_evidence_ref": source_evidence_ref,
                    "content_hash": dto.content_hash,
                    "mapping_version": dto.mapping_version,
                }
            )
        )
        evidence_model = self.env["marketing.attribution.evidence"].sudo()
        existing = evidence_model.search(
            [
                ("touchpoint_id", "=", touchpoint.id),
                ("source_evidence_ref", "=", source_evidence_ref),
                ("evidence_digest", "=", evidence_digest),
            ],
            limit=1,
        )
        if existing:
            return existing
        return (
            evidence_model.with_company(touchpoint.company_id)
            .with_context(
                marketing_attribution_write_token=MARKETING_ATTRIBUTION_WRITE_TOKEN
            )
            .create(
                {
                    "touchpoint_id": touchpoint.id,
                    "source_system": dto.source_system,
                    "source_evidence_ref": source_evidence_ref,
                    "evidence_digest": evidence_digest,
                    "mapping_version": dto.mapping_version,
                    "observed_at": dto.observed_at,
                    "disposition": disposition,
                    "related_touchpoint_id": previous.id or False,
                }
            )
        )

    @api.model
    def _result(self, touchpoint, disposition):
        return AttributionIngestResult(
            touchpoint_id=touchpoint.id,
            public_ref=touchpoint.public_ref,
            disposition=disposition,
            revision_sequence=touchpoint.revision_sequence,
            canonical_key=touchpoint.canonical_key,
            content_hash=touchpoint.content_hash,
        )

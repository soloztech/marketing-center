import datetime
import uuid

from odoo.tests.common import SavepointCase

from ..models.attribution_resolution_service import (
    MarketingAttributionAssetResolutionService,
)
from ..services.catalog_dto import ExternalEntityDTO
from ..services.dto import MarketingTouchpointDTO


class TestMarketingAttributionAssetResolutionBase(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.attribution_service = cls.env["marketing.attribution.service"]
        cls.catalog_service = cls.env["marketing.center.catalog.service"]
        cls.resolver = cls.env["marketing.attribution.asset.resolution.service"]

    def _touchpoint(self, asset_refs, occurrence_ref=None):
        return self.attribution_service._ingest_touchpoint(
            self.company,
            MarketingTouchpointDTO(
                source_system="test.asset.resolution.base",
                source_scope_ref="provider-neutral-suite",
                source_occurrence_ref=(
                    occurrence_ref or "resolution:%s" % uuid.uuid4()
                ),
                source_evidence_ref="evidence:%s" % uuid.uuid4(),
                occurred_at=datetime.datetime(2026, 9, 1, 12, 30),
                platform="test",
                channel="test",
                network="test",
                touchpoint_type="unknown",
                evidence_level="provider_asserted",
                asset_refs=asset_refs,
            ),
        )

    def _resolutions(self, result):
        return (
            self.env["marketing.attribution.asset.resolution"]
            .sudo()
            .search(
                [
                    ("company_id", "=", self.company.id),
                    ("canonical_key", "=", result.canonical_key),
                ]
            )
        )

    def test_base_registers_no_provider_asset_specs(self):
        self.assertEqual(
            MarketingAttributionAssetResolutionService._asset_resolver_specs(
                self.resolver
            ),
            {},
        )

    def test_unknown_namespace_is_explicitly_unsupported(self):
        result = self._touchpoint({"vendor.asset_id": "opaque-17"})

        resolution = self._resolutions(result)
        self.assertEqual(resolution.state, "unsupported")
        self.assertEqual(resolution.reason, "unsupported_namespace")
        self.assertEqual(resolution.target_kind, "unknown")
        self.assertFalse(resolution.provider_key)
        self.assertFalse(resolution.service_key)
        self.assertFalse(resolution.source_id)
        self.assertFalse(resolution.entity_id)

    def test_projection_tracks_effective_revision_without_provider_guessing(self):
        occurrence = "resolution-revision:%s" % uuid.uuid4()
        first = self._touchpoint(
            {"vendor.asset_id": "first"},
            occurrence_ref=occurrence,
        )
        row = self._resolutions(first)
        row_id = row.id

        second = self.attribution_service._ingest_touchpoint(
            self.company,
            MarketingTouchpointDTO(
                source_system="test.asset.resolution.base",
                source_scope_ref="provider-neutral-suite",
                source_occurrence_ref=occurrence,
                source_evidence_ref="evidence:%s" % uuid.uuid4(),
                occurred_at=datetime.datetime(2026, 9, 1, 12, 30),
                platform="test",
                channel="test",
                network="test",
                touchpoint_type="unknown",
                evidence_level="provider_asserted",
                revision_kind="enrichment",
                asset_refs={"vendor.asset_id": "second"},
            ),
        )

        current = self._resolutions(second)
        self.assertEqual(current.id, row_id)
        self.assertEqual(current.touchpoint_id.id, second.touchpoint_id)
        self.assertEqual(current.asset_value, "second")
        self.assertEqual(current.state, "unsupported")

    def test_catalog_retry_is_empty_without_provider_registration(self):
        account_ref = "account-%s" % uuid.uuid4().hex
        source = self.env["marketing.center.source"].create(
            {
                "name": "Provider-neutral source",
                "company_id": self.company.id,
                "service": "test.provider",
                "external_account_ref": account_ref,
                "external_account_id": account_ref,
                "currency_id": self.company.currency_id.id,
                "timezone": "UTC",
                "state": "active",
            }
        )
        result = self.catalog_service._upsert_entity(
            self.company,
            source,
            ExternalEntityDTO(
                name="Provider-neutral entity",
                entity_type="asset",
                external_ref="%s/assets/1" % account_ref,
                external_id="1",
                observed_at=datetime.datetime(2026, 9, 1, 12, 0),
            ),
        )
        entity = self.env["marketing.center.external.entity"].browse(result.entity_id)

        self.assertFalse(
            self.resolver._catalog_retry_candidates(source, entity, limit=100)
        )
        self.assertEqual(self.resolver._retry_after_catalog(source, entity), 0)

    def test_cron_backfill_reconstructs_missing_provider_neutral_projection(self):
        result = self._touchpoint({"vendor.asset_id": "opaque-18"})
        self.resolver._remove_projection(self.company.id, result.canonical_key)
        self.assertFalse(self._resolutions(result))

        self.assertEqual(
            self.resolver._cron_backfill(limit=1, retry_after_minutes=0),
            1,
        )
        self.assertEqual(self._resolutions(result).state, "unsupported")

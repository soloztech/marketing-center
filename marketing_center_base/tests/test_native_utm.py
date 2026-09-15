import datetime
import uuid
from unittest.mock import patch

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from ..models.attribution_resolution import ASSET_RESOLUTION_WRITE_TOKEN
from ..services.catalog_dto import ExternalEntityDTO
from ..services.dto import MarketingTouchpointDTO


class TestMarketingNativeUtm(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service = cls.env["marketing.native.utm.service"]
        cls.utm_source = cls.env["utm.source"].create(
            {"name": "Native UTM test source"}
        )
        cls.utm_medium = cls.env["utm.medium"].create(
            {"name": "Native UTM test medium"}
        )
        cls.source = cls.env["marketing.center.source"].create(
            {
                "name": "Native UTM source",
                "service": "test.ads",
                "external_account_ref": "account-1",
                "state": "active",
                "native_utm_mode": "apply",
                "native_utm_source_id": cls.utm_source.id,
                "native_utm_medium_id": cls.utm_medium.id,
            }
        )

    def _entity(self, source=None, **overrides):
        source = source if source is not None else self.source
        values = {
            "entity_type": "campaign",
            "external_ref": "account-1/campaigns/123",
            "external_id": "123",
            "name": "Externally managed campaign",
            "observed_at": datetime.datetime(2026, 9, 15, 12),
        }
        values.update(overrides)
        result = self.env["marketing.center.catalog.service"]._upsert_entity(
            source.company_id,
            source,
            ExternalEntityDTO(**values),
        )
        return self.env["marketing.center.external.entity"].browse(result.entity_id)

    def _touchpoint(self, entity, occurrence=None, **overrides):
        values = {
            "source_system": "test.native.utm",
            "source_scope_ref": "test-utm",
            "source_occurrence_ref": occurrence or str(uuid.uuid4()),
            "source_evidence_ref": str(uuid.uuid4()),
            "occurred_at": datetime.datetime(2026, 9, 15, 12),
            "platform": "test",
            "channel": "test",
            "network": "test",
            "touchpoint_type": "unknown",
            "evidence_level": "provider_asserted",
        }
        values.update(overrides)
        result = self.env["marketing.attribution.service"]._ingest_touchpoint(
            entity.company_id,
            MarketingTouchpointDTO(**values),
        )
        point = self.env["marketing.attribution.touchpoint"].browse(
            result.touchpoint_id
        )
        self.env["marketing.attribution.asset.resolution"].sudo().with_context(
            marketing_asset_resolution_write_token=ASSET_RESOLUTION_WRITE_TOKEN,
        ).create(
            {
                "company_id": entity.company_id.id,
                "touchpoint_id": point.id,
                "canonical_key": point.canonical_key,
                "asset_namespace": "test.campaign_id",
                "asset_value": entity.external_ref,
                "target_kind": "entity",
                "provider_key": "test",
                "service_key": entity.source_id.service,
                "mapped_entity_type": entity.entity_type,
                "canonical_external_ref": entity.external_ref,
                "source_id": entity.source_id.id,
                "entity_id": entity.id,
                "state": "resolved",
                "reason": "test_explicit_identity",
                "first_attempted_at": datetime.datetime(2026, 9, 15, 12),
                "last_attempted_at": datetime.datetime(2026, 9, 15, 12),
            }
        )
        return point

    def test_simulation_never_creates_campaign_or_mapping(self):
        entity = self._entity()
        point = self._touchpoint(entity)
        before = self.env["utm.campaign"].search_count([])
        result = self.service._resolve_touchpoint(point)
        self.assertEqual(result["state"], "would_create")
        self.assertFalse(entity.native_utm_campaign_id)
        self.assertFalse(entity.native_utm_mapping_origin)
        self.assertEqual(self.env["utm.campaign"].search_count([]), before)
        self.source.native_utm_mode = "simulate"
        result = self.service._resolve_touchpoint(point, apply=True)
        self.assertEqual(result["state"], "would_create")
        self.assertFalse(entity.native_utm_campaign_id)
        self.assertEqual(self.env["utm.campaign"].search_count([]), before)

    def test_create_replay_and_remote_rename_keep_native_identity(self):
        entity = self._entity()
        point = self._touchpoint(entity)
        first = self.service._resolve_touchpoint(point, apply=True)
        self.assertEqual(first["state"], "ready")
        self.assertTrue(first["created"])
        campaign = entity.native_utm_campaign_id
        self.assertEqual(campaign.title, entity.name)
        self.assertIn(entity.public_ref, campaign.name)
        self.assertEqual(entity.native_utm_mapping_origin, "automatic")
        replay = self.service._resolve_touchpoint(point, apply=True)
        self.assertEqual(replay["campaign_id"], first["campaign_id"])
        self.assertFalse(replay["created"])
        self._entity(name="Renamed in provider")
        self.assertEqual(
            self.service._resolve_touchpoint(point, apply=True)["campaign_id"],
            campaign.id,
        )
        self.assertEqual(campaign.title, "Externally managed campaign")

    def test_same_name_across_accounts_never_reuses_campaign(self):
        first_entity = self._entity()
        other = self.source.copy(
            {
                "external_account_ref": "account-2",
                "state": "active",
                "native_utm_mode": "apply",
                "native_utm_source_id": self.utm_source.id,
                "native_utm_medium_id": self.utm_medium.id,
            }
        )
        second_entity = self._entity(other, external_ref="account-2/campaigns/123")
        first = self.service._resolve_entity(first_entity, apply=True)
        second = self.service._resolve_entity(second_entity, apply=True)
        self.assertNotEqual(first["campaign_id"], second["campaign_id"])
        self.assertEqual(
            first_entity.native_utm_campaign_id.title,
            second_entity.native_utm_campaign_id.title,
        )
        self.assertNotEqual(
            first_entity.native_utm_campaign_id.name,
            second_entity.native_utm_campaign_id.name,
        )

    def test_manual_many_to_one_mapping_is_preserved(self):
        native = self.env["utm.campaign"].create({"title": "Shared reporting campaign"})
        first = self._entity()
        second = self._entity(external_ref="account-1/campaigns/456", external_id="456")
        (first | second).write({"native_utm_campaign_id": native.id})
        for entity in first | second:
            self.assertEqual(
                self.service._resolve_entity(entity, apply=True)["campaign_id"],
                native.id,
            )
            self.assertEqual(entity.native_utm_mapping_origin, "manual")

    def test_clearing_mapping_blocks_automatic_replacement(self):
        entity = self._entity()
        self.service._resolve_entity(entity, apply=True)
        before = self.env["utm.campaign"].search_count([])
        entity.native_utm_campaign_id = False
        self.assertTrue(entity.native_utm_blocked)
        self.assertEqual(
            self.service._resolve_entity(entity, apply=True)["state"], "disabled"
        )
        self.assertEqual(self.env["utm.campaign"].search_count([]), before)

    def test_disabled_source_and_disabled_autocreate(self):
        entity = self._entity()
        self.source.native_utm_mode = "disabled"
        self.assertEqual(
            self.service._resolve_entity(entity, apply=True)["state"], "disabled"
        )
        self.source.write(
            {"native_utm_mode": "apply", "native_utm_auto_create_campaign": False}
        )
        self.assertEqual(
            self.service._resolve_entity(entity, apply=True)["state"], "missing"
        )
        self.assertFalse(entity.native_utm_campaign_id)

    def test_removed_remote_campaign_preserves_mapping_without_classification(self):
        entity = self._entity()
        campaign_id = self.service._resolve_entity(entity, apply=True)["campaign_id"]
        self._entity(remote_missing_at=datetime.datetime(2026, 9, 16, 12))
        result = self.service._resolve_entity(entity, apply=True)
        self.assertEqual(result["reason"], "remote_campaign_missing")
        self.assertEqual(entity.native_utm_campaign_id.id, campaign_id)

    def test_remote_deleted_status_does_not_create_native_campaign(self):
        entity = self._entity(remote_status="REMOVED")
        self.assertEqual(
            self.service._resolve_entity(entity, apply=True)["reason"],
            "remote_campaign_inactive",
        )
        self.assertFalse(entity.native_utm_campaign_id)

    def test_configuration_requires_explicit_source_and_medium(self):
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.source.write({"native_utm_source_id": False})

    def test_catalog_fields_and_mapping_metadata_remain_protected(self):
        entity = self._entity()
        for values in (
            {"name": "Manual catalog overwrite"},
            {"native_utm_mapping_origin": "automatic"},
        ):
            with self.assertRaises(AccessError):
                entity.write(values)
        ad = self._entity(entity_type="ad", external_ref="account-1/ads/123")
        native = self.env["utm.campaign"].create({"title": "Invalid target"})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            ad.write({"native_utm_campaign_id": native.id})

    def test_cross_company_scope_is_refused_before_write(self):
        entity = self._entity()
        other = self.env["res.company"].create({"name": "Other native UTM company"})
        result = self.service.with_context(
            allowed_company_ids=[other.id]
        )._resolve_entity(entity, apply=True)
        self.assertEqual(result["reason"], "company_scope_mismatch")
        self.assertFalse(entity.native_utm_campaign_id)

    def test_non_effective_revision_is_not_classified(self):
        entity = self._entity()
        point = self._touchpoint(entity, occurrence="same-occurrence")
        self.env["marketing.attribution.service"]._ingest_touchpoint(
            entity.company_id,
            MarketingTouchpointDTO(
                source_system="test.native.utm",
                source_scope_ref="test-utm",
                source_occurrence_ref="same-occurrence",
                source_evidence_ref=str(uuid.uuid4()),
                occurred_at=datetime.datetime(2026, 9, 15, 12),
                platform="test",
                channel="test",
                network="test",
                touchpoint_type="unknown",
                evidence_level="provider_asserted",
                revision_kind="enrichment",
                asset_refs={"test.revision": "new"},
            ),
        )
        self.assertEqual(
            self.service._resolve_touchpoint(point, apply=True)["reason"],
            "effective_revision_required",
        )
        self.assertFalse(entity.native_utm_campaign_id)

    def test_conflicting_resolved_campaigns_are_not_guessed(self):
        first = self._entity()
        second = self._entity(external_ref="account-1/campaigns/456", external_id="456")
        point = self._touchpoint(first)
        row = self.env["marketing.attribution.asset.resolution"].search(
            [("touchpoint_id", "=", point.id)]
        )
        row.with_context(
            marketing_asset_resolution_write_token=ASSET_RESOLUTION_WRITE_TOKEN
        ).copy(
            {
                "asset_namespace": "test.other_campaign_id",
                "state": "resolved",
                "asset_value": second.external_ref,
                "entity_id": second.id,
                "canonical_external_ref": second.external_ref,
            }
        )
        self.assertEqual(
            self.service._resolve_touchpoint(point, apply=True)["state"], "conflict"
        )
        self.assertFalse((first | second).mapped("native_utm_campaign_id"))

    def test_resolver_change_emits_touchpoint_callback(self):
        entity = self._entity()
        with patch.object(
            type(self.service), "_after_native_utm_change", return_value=True
        ) as callback:
            point = self._touchpoint(entity)
        self.assertTrue(
            any(
                call.kwargs.get("touchpoint_ids") == [point.id]
                for call in callback.call_args_list
            )
        )

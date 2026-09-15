import datetime
import uuid

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.marketing_center_base.models.attribution_resolution import (
    ASSET_RESOLUTION_WRITE_TOKEN,
)
from odoo.addons.marketing_center_base.services import MarketingTouchpointDTO
from odoo.addons.marketing_center_base.services.catalog_dto import ExternalEntityDTO


class TestCrmNativeUtm(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.classifier = cls.env["marketing.crm.native.utm.service"]
        cls.crm = cls.env["marketing.crm.service"]
        cls.utm_source = cls.env["utm.source"].create({"name": "Native CRM source"})
        cls.utm_medium = cls.env["utm.medium"].create({"name": "Native CRM medium"})
        cls.source = cls.env["marketing.center.source"].create(
            {
                "name": "Native CRM source",
                "service": "test.ads",
                "state": "active",
                "external_account_ref": "crm-test",
                "native_utm_mode": "apply",
                "native_utm_source_id": cls.utm_source.id,
                "native_utm_medium_id": cls.utm_medium.id,
            }
        )
        cls.lead = cls.env["crm.lead"].create(
            {
                "name": "Native UTM qualification",
                "type": "lead",
                "company_id": cls.env.company.id,
                "campaign_id": False,
                "source_id": False,
                "medium_id": False,
                "team_id": False,
                "user_id": False,
            }
        )

    def _point(self, external_id="123"):
        result = self.env["marketing.center.catalog.service"]._upsert_entity(
            self.env.company,
            self.source,
            ExternalEntityDTO(
                entity_type="campaign",
                external_ref="campaigns/" + external_id,
                external_id=external_id,
                name="Remote campaign " + external_id,
                observed_at=datetime.datetime(2026, 9, 15, 12),
            ),
        )
        entity = self.env["marketing.center.external.entity"].browse(result.entity_id)
        result = self.env["marketing.attribution.service"]._ingest_touchpoint(
            self.env.company,
            MarketingTouchpointDTO(
                source_system="test.native.crm",
                source_scope_ref="native-crm",
                source_occurrence_ref=str(uuid.uuid4()),
                occurred_at=datetime.datetime(2026, 9, 15, 12),
                platform="test",
                channel="test",
                touchpoint_type="entry_point",
                evidence_level="provider_asserted",
            ),
        )
        point = self.env["marketing.attribution.touchpoint"].browse(
            result.touchpoint_id
        )
        self.env["marketing.attribution.asset.resolution"].with_context(
            marketing_asset_resolution_write_token=ASSET_RESOLUTION_WRITE_TOKEN
        ).create(
            {
                "company_id": self.env.company.id,
                "touchpoint_id": point.id,
                "canonical_key": point.canonical_key,
                "asset_namespace": "test.campaign_id",
                "asset_value": entity.external_ref,
                "target_kind": "entity",
                "provider_key": "test",
                "service_key": "test.ads",
                "mapped_entity_type": "campaign",
                "canonical_external_ref": entity.external_ref,
                "source_id": self.source.id,
                "entity_id": entity.id,
                "state": "resolved",
                "reason": "test_identity",
                "first_attempted_at": datetime.datetime(2026, 9, 15, 12),
                "last_attempted_at": datetime.datetime(2026, 9, 15, 12),
            }
        )
        link = self.crm._link_touchpoint_lead(
            point, self.lead, authority_key="test", authority_ref="test"
        )
        return entity, point, link

    def test_preview_creates_neither_campaign_receipt_nor_native_values(self):
        entity, _, _ = self._point()
        before = self.env["utm.campaign"].search_count([])
        result = self.classifier._classify(self.lead)
        self.assertEqual(result["state"], "simulation")
        self.assertFalse(entity.native_utm_campaign_id)
        self.assertFalse(self.lead.marketing_utm_application_ids)
        self.assertEqual(self.env["utm.campaign"].search_count([]), before)
        self.assertFalse(any(self.lead._native_utm_values().values()))

    def test_automatic_creation_native_tuple_and_replay(self):
        entity, _, _ = self._point()
        result = self.classifier._classify(self.lead, apply=True)
        self.assertEqual(result["state"], "applied")
        campaign = entity.native_utm_campaign_id
        self.assertEqual(self.lead.campaign_id, campaign)
        self.assertEqual(self.lead.source_id, self.utm_source)
        self.assertEqual(self.lead.medium_id, self.utm_medium)
        receipt = self.lead.marketing_utm_receipt_id
        self.classifier._classify(self.lead, apply=True)
        count = len(self.lead.marketing_utm_application_ids)
        self.classifier._classify(self.lead, apply=True)
        self.assertEqual(len(self.lead.marketing_utm_application_ids), count)
        self.assertEqual(self.lead.marketing_utm_receipt_id, receipt)
        self.assertEqual(entity.native_utm_campaign_id, campaign)
        self.assertFalse(self.lead.user_id)
        self.assertFalse(self.lead.team_id)
        self.assertEqual(self.lead.type, "lead")

    def test_simulation_source_does_not_create_even_in_worker(self):
        entity, _, _ = self._point()
        self.source.native_utm_mode = "simulate"
        self.assertEqual(
            self.classifier._classify(self.lead, apply=True)["state"], "simulation"
        )
        self.assertFalse(entity.native_utm_campaign_id)
        self.assertFalse(self.lead.campaign_id)

    def test_existing_native_partial_tuple_is_preserved(self):
        entity, _, _ = self._point()
        lead = self.env["crm.lead"].create(
            {
                "name": "Existing native",
                "company_id": self.env.company.id,
                "medium_id": self.env.ref("utm.utm_medium_website").id,
                "campaign_id": False,
                "source_id": False,
            }
        )
        self.crm._link_touchpoint_lead(
            self.lead.marketing_attribution_link_ids.touchpoint_id, lead
        )
        self.assertEqual(
            self.classifier._classify(lead, apply=True)["state"], "conflict"
        )
        self.assertFalse(entity.native_utm_campaign_id)
        self.assertEqual(lead.medium_id, self.env.ref("utm.utm_medium_website"))

    def test_proven_website_default_can_be_replaced(self):
        _, point, _ = self._point()
        lead = self.env["crm.lead"].create(
            {
                "name": "Proven website default",
                "company_id": self.env.company.id,
                "medium_id": self.env.ref("utm.utm_medium_website").id,
                "campaign_id": False,
                "source_id": False,
            }
        )
        lead._mark_native_utm_website_default()
        self.crm._link_touchpoint_lead(point, lead)
        self.assertEqual(
            self.classifier._classify(lead, apply=True)["state"], "applied"
        )
        self.assertEqual(lead.medium_id, self.utm_medium)

    def test_manual_edit_even_same_value_blocks_future_writer(self):
        _, _, _ = self._point()
        self.classifier._classify(self.lead, apply=True)
        self.lead.write({"campaign_id": self.lead.campaign_id.id})
        old = self.lead._native_utm_values()
        self.assertEqual(
            self.classifier._classify(self.lead, apply=True)["state"], "manual"
        )
        self.assertEqual(self.lead._native_utm_values(), old)
        with self.assertRaises(ValidationError):
            self.lead.action_revert_native_utm()

    def test_ambiguous_campaigns_do_not_create_or_classify(self):
        first, _, _ = self._point("123")
        second, _, _ = self._point("456")
        self.assertEqual(
            self.classifier._classify(self.lead, apply=True)["state"], "conflict"
        )
        self.assertFalse(first.native_utm_campaign_id)
        self.assertFalse(second.native_utm_campaign_id)
        self.assertFalse(self.lead.campaign_id)

    def test_multiple_points_same_campaign_are_idempotent(self):
        entity, _, _ = self._point()
        self._point()
        self.assertEqual(
            self.classifier._classify(self.lead, apply=True)["state"], "applied"
        )
        self.assertEqual(self.lead.campaign_id, entity.native_utm_campaign_id)

    def test_revocation_removes_owned_classification_only(self):
        _, _, link = self._point()
        self.classifier._classify(self.lead, apply=True)
        self.crm._revoke_attribution_assertions(link, "test", "test", "withdraw")
        result = self.classifier._classify(self.lead, apply=True)
        self.assertEqual(result["state"], "revoked")
        self.assertFalse(any(self.lead._native_utm_values().values()))

    def test_safe_undo_restores_before_without_recreating_on_retry(self):
        self._point()
        self.classifier._classify(self.lead, apply=True)
        self.lead.action_revert_native_utm()
        self.assertFalse(any(self.lead._native_utm_values().values()))
        self.assertTrue(self.lead.marketing_utm_manual)
        self.classifier._classify(self.lead, apply=True)
        self.assertFalse(self.lead.campaign_id)

    def test_remapping_then_revocation_restores_original_baseline(self):
        entity, _, link = self._point()
        self.classifier._classify(self.lead, apply=True)
        replacement = self.env["utm.campaign"].create({"title": "Reviewed campaign"})
        entity.native_utm_campaign_id = replacement
        self.classifier._classify(self.lead, apply=True)
        self.assertEqual(self.lead.campaign_id, replacement)
        self.crm._revoke_attribution_assertions(link, "test", "test", "withdraw")
        self.classifier._classify(self.lead, apply=True)
        self.assertFalse(any(self.lead._native_utm_values().values()))

    def test_new_conflicting_evidence_removes_only_owned_tuple(self):
        self._point("123")
        self.classifier._classify(self.lead, apply=True)
        self._point("456")
        result = self.classifier._classify(self.lead, apply=True)
        self.assertEqual(result["state"], "conflict")
        self.assertFalse(any(self.lead._native_utm_values().values()))

    def test_simulation_never_restores_baseline_when_evidence_conflicts(self):
        self._point("123")
        self.classifier._classify(self.lead, apply=True)
        before = self.lead._native_utm_values()
        self.source.native_utm_mode = "simulate"
        self._point("456")
        result = self.classifier._classify(self.lead, apply=True)
        self.assertEqual(result["state"], "simulation")
        self.assertEqual(self.lead._native_utm_values(), before)

    def test_internal_provenance_and_receipts_cannot_be_forged(self):
        with self.assertRaises(AccessError):
            self.lead.with_context(marketing_crm_utm_token=True).write(
                {"marketing_utm_manual": False}
            )
        with self.assertRaises(AccessError):
            self.env["crm.lead"].with_context(
                default_marketing_utm_default_json={}
            ).create({"name": "Forged"})
        self._point()
        self.classifier._classify(self.lead, apply=True)
        with self.assertRaises(AccessError):
            self.lead.marketing_utm_receipt_id.sudo().write({"reason": "forged"})

    def test_change_hook_and_persisted_scope_reconcile(self):
        entity, _, _ = self._point()
        scope = {"source_ids": [self.source.id], "entity_ids": [], "touchpoint_ids": []}
        self.assertEqual(self.env.company._job_reconcile_native_utm(scope), 1)
        jobs = self.env["queue.job"].search(
            [
                ("model_name", "=", "crm.lead"),
                ("method_name", "=", "_job_resolve_native_utm"),
            ]
        )
        self.assertTrue(jobs)
        self.assertFalse(entity.native_utm_campaign_id)

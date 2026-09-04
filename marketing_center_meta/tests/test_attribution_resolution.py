import datetime
import uuid

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests.common import SavepointCase

from odoo.addons.marketing_center_base.services.catalog_dto import ExternalEntityDTO
from odoo.addons.marketing_center_base.services.dto import MarketingTouchpointDTO


class TestMarketingAttributionAssetResolution(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.attribution_service = cls.env["marketing.attribution.service"]
        cls.catalog_service = cls.env["marketing.center.catalog.service"]
        cls.resolver = cls.env["marketing.attribution.asset.resolution.service"]

    def _source(self, account_id, company=None, name=None):
        company = company or self.company
        return (
            self.env["marketing.center.source"]
            .sudo()
            .with_context(
                allowed_company_ids=list(
                    {self.company.id, company.id, self.env.company.id}
                )
            )
            .create(
                {
                    "name": name or "Meta %s" % account_id,
                    "company_id": company.id,
                    "service": "meta.ads",
                    "external_account_ref": "act_%s" % account_id,
                    "external_account_id": str(account_id),
                    "currency_id": company.currency_id.id,
                    "timezone": "UTC",
                    "state": "active",
                }
            )
        )

    def _entity(self, source, entity_type, segment, object_id):
        company = source.company_id
        result = self.catalog_service.with_context(
            allowed_company_ids=list({self.company.id, company.id})
        )._upsert_entity(
            company,
            source,
            ExternalEntityDTO(
                entity_type=entity_type,
                external_ref="%s/%s/%s"
                % (source.external_account_ref, segment, object_id),
                external_id=str(object_id),
                name="%s %s" % (entity_type, object_id),
                observed_at=datetime.datetime(2026, 9, 1, 12, 0),
            ),
        )
        return self.env["marketing.center.external.entity"].browse(result.entity_id)

    def _touchpoint(self, asset_refs, company=None, occurrence_ref=None):
        company = company or self.company
        result = self.attribution_service.with_context(
            allowed_company_ids=list({self.company.id, company.id})
        )._ingest_touchpoint(
            company,
            MarketingTouchpointDTO(
                source_system="test.asset.resolution",
                source_scope_ref="resolution-suite",
                source_occurrence_ref=(
                    occurrence_ref or "resolution:%s" % uuid.uuid4()
                ),
                source_evidence_ref="evidence:%s" % uuid.uuid4(),
                occurred_at=datetime.datetime(2026, 9, 1, 12, 30),
                platform="meta",
                channel="paid_social",
                network="meta",
                touchpoint_type="paid_ad_signal",
                evidence_level="provider_asserted",
                asset_refs=asset_refs,
            ),
        )
        return result

    def _resolutions(self, result, company=None):
        company = company or self.company
        return (
            self.env["marketing.attribution.asset.resolution"]
            .sudo()
            .search(
                [
                    ("company_id", "=", company.id),
                    ("canonical_key", "=", result.canonical_key),
                ]
            )
        )

    def test_meta_addon_registers_its_public_asset_namespaces(self):
        specs = self.resolver._asset_resolver_specs()

        self.assertEqual(specs["meta.ad_account_id"].target_kind, "source")
        self.assertEqual(specs["meta.ad_id"].entity_type, "ad")
        self.assertEqual(specs["meta.ad_id"].ref_segment, "ads")
        self.assertTrue(specs["meta.source_id"].polymorphic)
        self.assertEqual(
            {
                spec.provider_key
                for spec in specs.values()
                if spec.service_key == "meta.ads"
            },
            {"meta"},
        )

    def test_meta_typed_references_resolve_to_canonical_catalog_entities(self):
        source = self._source("123")
        expected = {
            "meta.campaign_id": self._entity(source, "campaign", "campaigns", "10"),
            "meta.adset_id": self._entity(source, "group", "adsets", "20"),
            "meta.ad_id": self._entity(source, "ad", "ads", "30"),
            "meta.creative_id": self._entity(source, "creative", "creatives", "40"),
            "meta.form_id": self._entity(source, "form", "forms", "50"),
        }
        result = self._touchpoint(
            {
                "meta.ad_account_id": "123",
                "meta.campaign_id": "10",
                "meta.adset_id": "20",
                "meta.ad_id": "30",
                "meta.creative_id": "40",
                "meta.form_id": "50",
            }
        )

        resolutions = self._resolutions(result)
        self.assertEqual(len(resolutions), 6)
        self.assertEqual(set(resolutions.mapped("state")), {"resolved"})
        account = resolutions.filtered(
            lambda item: item.asset_namespace == "meta.ad_account_id"
        )
        self.assertEqual(account.source_id, source)
        self.assertFalse(account.entity_id)
        for namespace, entity in expected.items():
            resolution = resolutions.filtered(
                lambda item, key=namespace: item.asset_namespace == key
            )
            self.assertEqual(resolution.source_id, source)
            self.assertEqual(resolution.entity_id, entity)
            self.assertEqual(resolution.canonical_external_ref, entity.external_ref)

    def test_missing_catalog_entity_is_explicitly_unresolved(self):
        source = self._source("201")
        result = self._touchpoint({"meta.ad_account_id": "201", "meta.ad_id": "999"})

        resolution = self._resolutions(result).filtered(
            lambda item: item.asset_namespace == "meta.ad_id"
        )
        self.assertEqual(resolution.state, "unresolved")
        self.assertEqual(resolution.reason, "entity_not_in_catalog")
        self.assertEqual(resolution.source_id, source)
        self.assertFalse(resolution.entity_id)
        self.assertEqual(resolution.canonical_external_ref, "act_201/ads/999")

    def test_duplicate_cross_account_id_is_ambiguous_without_account_hint(self):
        first = self._source("301")
        second = self._source("302")
        self._entity(first, "ad", "ads", "777")
        self._entity(second, "ad", "ads", "777")

        result = self._touchpoint({"meta.ad_id": "777"})

        resolution = self._resolutions(result)
        self.assertEqual(resolution.state, "ambiguous")
        self.assertEqual(resolution.reason, "multiple_entities")
        self.assertFalse(resolution.source_id)
        self.assertFalse(resolution.entity_id)

    def test_unhinted_lookup_finds_unique_entity_beyond_source_sample(self):
        sources = [self._source("31%s" % index) for index in range(1, 5)]
        expected = self._entity(sources[-1], "ad", "ads", "778")

        result = self._touchpoint({"meta.ad_id": "778"})

        resolution = self._resolutions(result)
        self.assertEqual(resolution.state, "resolved")
        self.assertEqual(resolution.source_id, sources[-1])
        self.assertEqual(resolution.entity_id, expected)

    def test_more_than_three_unhinted_candidates_remain_ambiguous(self):
        sources = [self._source("32%s" % index) for index in range(1, 5)]
        for source in sources:
            self._entity(source, "ad", "ads", "779")

        result = self._touchpoint({"meta.ad_id": "779"})

        resolution = self._resolutions(result)
        self.assertEqual(resolution.state, "ambiguous")
        self.assertEqual(resolution.reason, "multiple_entities")
        self.assertGreater(resolution.entity_candidate_count, 1)
        self.assertFalse(resolution.entity_id)

    def test_unknown_namespace_is_unsupported_and_does_not_guess(self):
        result = self._touchpoint({"other.vendor_object": "opaque-17"})

        resolution = self._resolutions(result)
        self.assertEqual(resolution.state, "unsupported")
        self.assertEqual(resolution.reason, "unsupported_namespace")
        self.assertEqual(resolution.target_kind, "unknown")
        self.assertFalse(resolution.source_id)
        self.assertFalse(resolution.entity_id)

    def test_catalog_arrival_retries_unresolved_projection_idempotently(self):
        source = self._source("401")
        result = self._touchpoint({"meta.ad_account_id": "401", "meta.ad_id": "88"})
        before = self._resolutions(result).filtered(
            lambda item: item.asset_namespace == "meta.ad_id"
        )
        before_id = before.id
        self.assertEqual(before.state, "unresolved")

        entity = self._entity(source, "ad", "ads", "88")

        after = self._resolutions(result).filtered(
            lambda item: item.asset_namespace == "meta.ad_id"
        )
        self.assertEqual(after.id, before_id)
        self.assertEqual(after.state, "resolved")
        self.assertEqual(after.entity_id, entity)
        self.assertGreater(after.attempt_count, 1)
        self.resolver._resolve_canonical_key(self.company.id, result.canonical_key)
        self.assertEqual(
            len(
                self._resolutions(result).filtered(
                    lambda item: item.asset_namespace == "meta.ad_id"
                )
            ),
            1,
        )

    def test_cron_backfill_is_bounded_and_reconstructs_missing_rows(self):
        source = self._source("451")
        self._entity(source, "ad", "ads", "1")
        self._entity(source, "ad", "ads", "2")
        first = self._touchpoint({"meta.ad_account_id": "451", "meta.ad_id": "1"})
        second = self._touchpoint({"meta.ad_account_id": "451", "meta.ad_id": "2"})
        self.resolver._remove_projection(self.company.id, first.canonical_key)
        self.resolver._remove_projection(self.company.id, second.canonical_key)
        self.assertFalse(self._resolutions(first) | self._resolutions(second))

        self.assertEqual(
            self.resolver._cron_backfill(limit=1, retry_after_minutes=0), 1
        )
        rebuilt = bool(self._resolutions(first)) + bool(self._resolutions(second))
        self.assertEqual(rebuilt, 1)
        self.assertEqual(
            self.resolver._cron_backfill(limit=1, retry_after_minutes=0), 1
        )
        self.assertTrue(self._resolutions(first))
        self.assertTrue(self._resolutions(second))

    def test_meta_source_id_resolves_only_one_polymorphic_candidate(self):
        source = self._source("501")
        entity = self._entity(source, "ad", "ads", "909")
        result = self._touchpoint(
            {"meta.ad_account_id": "501", "meta.source_id": "909"}
        )

        resolution = self._resolutions(result).filtered(
            lambda item: item.asset_namespace == "meta.source_id"
        )
        self.assertEqual(resolution.state, "resolved")
        self.assertEqual(resolution.entity_id, entity)
        self.assertEqual(resolution.mapped_entity_type, "ad")
        self.assertEqual(resolution.reason, "unique_polymorphic_entity")

    def test_projection_tracks_effective_revision_without_duplicate_rows(self):
        source = self._source("601")
        first_entity = self._entity(source, "ad", "ads", "1")
        second_entity = self._entity(source, "ad", "ads", "2")
        occurrence = "resolution-revision:%s" % uuid.uuid4()
        first = self._touchpoint(
            {"meta.ad_account_id": "601", "meta.ad_id": "1"},
            occurrence_ref=occurrence,
        )
        row = self._resolutions(first).filtered(
            lambda item: item.asset_namespace == "meta.ad_id"
        )
        row_id = row.id
        self.assertEqual(row.entity_id, first_entity)

        second = self.attribution_service._ingest_touchpoint(
            self.company,
            MarketingTouchpointDTO(
                source_system="test.asset.resolution",
                source_scope_ref="resolution-suite",
                source_occurrence_ref=occurrence,
                source_evidence_ref="evidence:%s" % uuid.uuid4(),
                occurred_at=datetime.datetime(2026, 9, 1, 12, 30),
                platform="meta",
                channel="paid_social",
                network="meta",
                touchpoint_type="paid_ad_signal",
                evidence_level="provider_asserted",
                revision_kind="enrichment",
                asset_refs={"meta.ad_account_id": "601", "meta.ad_id": "2"},
            ),
        )

        current = self._resolutions(second).filtered(
            lambda item: item.asset_namespace == "meta.ad_id"
        )
        self.assertEqual(current.id, row_id)
        self.assertEqual(current.touchpoint_id.id, second.touchpoint_id)
        self.assertEqual(current.entity_id, second_entity)

    def test_resolution_never_crosses_company_boundaries(self):
        other_company = self.env["res.company"].create(
            {"name": "Asset Resolution Other %s" % uuid.uuid4()}
        )
        own_source = self._source("701")
        other_source = self._source("701", company=other_company)
        self._entity(own_source, "ad", "ads", "42")
        other_entity = self._entity(other_source, "ad", "ads", "42")

        result = self._touchpoint(
            {"meta.ad_account_id": "701", "meta.ad_id": "42"},
            company=other_company,
        )

        resolution = self._resolutions(result, company=other_company).filtered(
            lambda item: item.asset_namespace == "meta.ad_id"
        )
        self.assertEqual(resolution.company_id, other_company)
        self.assertEqual(resolution.source_id, other_source)
        self.assertEqual(resolution.entity_id, other_entity)

    def test_roster_acl_exposes_only_resolutions_of_visible_sources(self):
        visible_source = self._source("801")
        hidden_source = self._source("802")
        self._entity(visible_source, "ad", "ads", "1")
        self._entity(hidden_source, "ad", "ads", "2")
        visible_result = self._touchpoint(
            {"meta.ad_account_id": "801", "meta.ad_id": "1"}
        )
        hidden_result = self._touchpoint(
            {"meta.ad_account_id": "802", "meta.ad_id": "2"}
        )
        viewer_group = self.env.ref(
            "marketing_center_base.group_marketing_center_viewer"
        )
        viewer = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Resolution roster viewer",
                    "login": "resolution-viewer-%s" % uuid.uuid4(),
                    "company_id": self.company.id,
                    "company_ids": [Command.set(self.company.ids)],
                    "groups_id": [Command.set(viewer_group.ids)],
                }
            )
        )
        team = self.env["marketing.center.team"].create(
            {"name": "Resolution roster", "company_id": self.company.id}
        )
        self.env["marketing.center.team.member"].create(
            {"team_id": team.id, "user_id": viewer.id, "role": "viewer"}
        )
        self.env["marketing.center.team.source"].create(
            {
                "team_id": team.id,
                "source_id": visible_source.id,
                "access_mode": "read",
            }
        )

        rows = (
            self.env["marketing.attribution.asset.resolution"]
            .with_user(viewer)
            .search([])
        )
        self.assertEqual(
            set(rows.mapped("canonical_key")), {visible_result.canonical_key}
        )
        hidden = self._resolutions(hidden_result).filtered("entity_id")
        with self.assertRaises(AccessError):
            hidden.with_user(viewer).check_access_rule("read")

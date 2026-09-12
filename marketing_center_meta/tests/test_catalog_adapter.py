import datetime
from types import SimpleNamespace
from unittest.mock import patch

from odoo.tests.common import SavepointCase

from odoo.addons.meta_api_base.services.errors import MetaApiError

from ..services.catalog import (
    META_CATALOG_ENTITY_TYPES,
    decode_meta_catalog_cursor,
    fetch_meta_catalog_page,
    meta_catalog_sweep_reporting_context,
    orchestrate_meta_catalog_page,
)
from ..services.graph_contract import META_MARKETING_GRAPH_VERSION


class TestMetaCatalogAdapter(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = SimpleNamespace(graph_version="v26.0")
        cls.observed_at = datetime.datetime(2026, 8, 31, 12, 0)
        cls.context_hash = "a" * 64

    def _fetch(self, entity_type, payload, after=""):
        with patch(
            "odoo.addons.marketing_center_meta.services.catalog.graph_request",
            return_value=payload,
        ) as request:
            page = fetch_meta_catalog_page(
                self.app,
                "synthetic-token",
                "act_123",
                entity_type,
                after=after,
                reporting_context_hash=self.context_hash,
                observed_at=self.observed_at,
            )
        return page, request

    def test_campaign_is_allowlisted_and_normalized(self):
        page, request = self._fetch(
            "campaign",
            {
                "data": [
                    {
                        "id": "10",
                        "name": "Campaign A",
                        "status": "PAUSED",
                        "effective_status": "ACTIVE",
                        "objective": "OUTCOME_SALES",
                        "daily_budget": "001500",
                        "lifetime_budget": 9000,
                        "special_ad_categories": ["HOUSING", "CREDIT"],
                        "created_time": "2026-08-30T10:00:00+0000",
                        "updated_time": "2026-08-31T11:00:00Z",
                        "targeting": {"geo_locations": {"countries": ["BR"]}},
                        "signed_url": "https://example.invalid/private",
                    }
                ]
            },
        )
        entity = page.items[0]
        self.assertEqual(entity.external_ref, "act_123/campaigns/10")
        self.assertEqual(entity.remote_status, "active")
        self.assertEqual(entity.attributes["meta.daily_budget"], 1500)
        self.assertEqual(entity.attributes["meta.lifetime_budget"], 9000)
        self.assertEqual(
            entity.attributes["meta.special_ad_categories"],
            ["credit", "housing"],
        )
        self.assertNotIn("targeting", str(entity.attributes))
        self.assertNotIn("url", str(entity.attributes).lower())
        self.assertEqual(
            entity.provider_updated_at,
            datetime.datetime(2026, 8, 31, 11, 0),
        )
        self.assertEqual(request.call_args.args[2], "GET")
        self.assertEqual(request.call_args.args[3], "act_123/campaigns")
        self.assertNotIn("targeting", request.call_args.kwargs["params"]["fields"])

    def test_group_and_ad_hierarchy_use_complete_references(self):
        group_page, _request = self._fetch(
            "group",
            {
                "data": [
                    {
                        "id": "20",
                        "campaign_id": "10",
                        "name": "Ad set A",
                        "status": "ACTIVE",
                        "bid_amount": "250",
                    }
                ]
            },
        )
        group = group_page.items[0]
        self.assertEqual(group.entity_type, "group")
        self.assertEqual(group.group_type, "meta_adset")
        self.assertEqual(group.external_ref, "act_123/adsets/20")
        self.assertEqual(group.parent_entity_type, "campaign")
        self.assertEqual(group.parent_external_ref, "act_123/campaigns/10")

        ad_page, _request = self._fetch(
            "ad",
            {
                "data": [
                    {
                        "id": "30",
                        "adset_id": "20",
                        "campaign_id": "10",
                        "name": "Ad A",
                        "effective_status": "WITH_ISSUES",
                        "creative": {"id": "40", "ignored": "payload"},
                    }
                ]
            },
        )
        ad = ad_page.items[0]
        self.assertEqual(ad.external_ref, "act_123/ads/30")
        self.assertEqual(ad.parent_entity_type, "group")
        self.assertEqual(ad.parent_external_ref, "act_123/adsets/20")
        self.assertEqual(ad.attributes["meta.campaign_ref"], "act_123/campaigns/10")
        self.assertEqual(ad.attributes["meta.creative_ref"], "act_123/creatives/40")

    def test_creative_is_reusable_root_with_safe_identifiers_only(self):
        page, request = self._fetch(
            "creative",
            {
                "data": [
                    {
                        "id": "40",
                        "name": "Creative A",
                        "status": "ACTIVE",
                        "object_type": "VIDEO",
                        "image_hash": "abc123",
                        "video_id": "50",
                        "object_story_id": "60_70",
                        "effective_object_story_id": "60_71",
                        "instagram_user_id": "80",
                        "object_story_spec": {"page_id": "60"},
                        "thumbnail_url": "https://example.invalid/signed",
                    }
                ]
            },
        )
        creative = page.items[0]
        self.assertEqual(creative.external_ref, "act_123/creatives/40")
        self.assertFalse(creative.parent_external_ref)
        self.assertEqual(creative.attributes["meta.video_id"], "50")
        self.assertEqual(creative.attributes["meta.instagram_user_id"], "80")
        self.assertNotIn("spec", str(creative.attributes).lower())
        fields = request.call_args.kwargs["params"]["fields"]
        self.assertNotIn("object_story_spec", fields)
        self.assertNotIn("thumbnail_url", fields)

    def test_multiline_creative_names_do_not_abort_a_catalog_page(self):
        page, _request = self._fetch(
            "creative",
            {
                "data": [
                    {"id": "40", "name": "First line\n\nSecond line"},
                    {"id": "41", "name": "Other\r\nline\twith tab"},
                    {"id": "42", "name": "Already  valid name"},
                ]
            },
        )
        self.assertEqual(
            [item.name for item in page.items],
            ["First line Second line", "Other line with tab", "Already  valid name"],
        )
        self.assertEqual(
            [item.external_ref for item in page.items],
            ["act_123/creatives/40", "act_123/creatives/41", "act_123/creatives/42"],
        )

    def test_name_normalization_preserves_bounds_and_identifier_validation(self):
        for row in (
            {"id": "40", "name": "Bad\x00name"},
            {"id": "40", "name": "Bad\x1bname"},
            {"id": "40", "name": "x" * 1025},
            {"id": "40", "name": 123},
            {"id": "4\n0", "name": "Valid name"},
            {"id": "40", "video_id": "5\n0", "name": "Valid name"},
        ):
            with self.subTest(row=row):
                with self.assertRaises(MetaApiError):
                    self._fetch("creative", {"data": [row]})

    def test_single_run_cursor_orders_every_stage(self):
        terminal_campaign, _request = self._fetch("campaign", {"data": []})
        campaign_page = orchestrate_meta_catalog_page(
            "campaign",
            terminal_campaign,
        )
        self.assertTrue(campaign_page.has_more)
        self.assertEqual(
            decode_meta_catalog_cursor(campaign_page.next_cursor),
            ("group", ""),
        )

        creative_terminal, _request = self._fetch("creative", {"data": []})
        creative_page = orchestrate_meta_catalog_page(
            "creative",
            creative_terminal,
        )
        self.assertFalse(creative_page.has_more)
        self.assertFalse(creative_page.next_cursor)
        self.assertFalse(creative_page.authoritative_complete)

    def test_provider_cursor_is_wrapped_and_next_url_is_never_followed(self):
        provider_page, request = self._fetch(
            "ad",
            {
                "data": [],
                "paging": {
                    "cursors": {"after": "opaque-page-2"},
                    "next": "https://graph.facebook.com/private-next-url",
                },
            },
            after="opaque-page-1",
        )
        page = orchestrate_meta_catalog_page("ad", provider_page)
        self.assertEqual(
            decode_meta_catalog_cursor(page.next_cursor),
            ("ad", "opaque-page-2"),
        )
        self.assertEqual(
            request.call_args.kwargs["params"]["after"],
            "opaque-page-1",
        )
        self.assertNotIn("private-next-url", page.next_cursor)

    def test_cursor_without_next_is_terminal_for_the_edge(self):
        provider_page, request = self._fetch(
            "ad",
            {"data": [], "paging": {"cursors": {"after": "unused"}}},
        )
        request.assert_called_once()
        page = orchestrate_meta_catalog_page("ad", provider_page)
        self.assertEqual(decode_meta_catalog_cursor(page.next_cursor), ("creative", ""))

    def test_invalid_paging_cursor_and_cursor_envelope_are_rejected(self):
        invalid_pages = (
            {"data": [], "paging": []},
            {"data": [], "paging": 0},
            {"data": [], "paging": {"cursors": []}},
            {"data": [], "paging": {"cursors": 0}},
            {"data": [], "paging": {"next": []}},
            {
                "data": [],
                "paging": {"next": "private", "cursors": {"after": "same"}},
            },
        )
        for payload in invalid_pages:
            with self.assertRaises(MetaApiError):
                self._fetch("campaign", payload, after="same")
        for value in (
            "not-json",
            '{"version":1,"stage":"unknown","after":""}',
            '{"version":2,"stage":"campaign","after":""}',
            '{"version":1,"stage":"campaign","after":"","extra":true}',
        ):
            with self.assertRaises(MetaApiError):
                decode_meta_catalog_cursor(value)

    def test_invalid_timestamp_budget_and_category_are_rejected(self):
        invalid_rows = (
            {"id": "10", "updated_time": "2026-08-31T11:00:00"},
            {"id": "10", "daily_budget": "12.50"},
            {"id": "10", "lifetime_budget": -1},
            {"id": "10", "special_ad_categories": [""]},
        )
        for row in invalid_rows:
            with self.assertRaises(MetaApiError):
                self._fetch("campaign", {"data": [row]})

    def test_context_commits_to_full_allowlisted_contract(self):
        context = meta_catalog_sweep_reporting_context("v26.0", "act_123")
        self.assertEqual(
            [stage["entity_type"] for stage in context["stages"]],
            list(META_CATALOG_ENTITY_TYPES),
        )
        self.assertEqual(context["provider"], "meta")
        rendered = str(context)
        self.assertNotIn("targeting", rendered)
        self.assertNotIn("url", rendered.lower())

    def test_catalog_shares_the_explicit_marketing_graph_contract(self):
        self.assertEqual(META_MARKETING_GRAPH_VERSION, "v26.0")
        incompatible = SimpleNamespace(graph_version="v27.0")
        with self.assertRaisesRegex(
            MetaApiError,
            "Catalog supports Graph v26.0.*configured as v27.0",
        ):
            with patch(
                "odoo.addons.marketing_center_meta.services.catalog.graph_request"
            ) as request:
                fetch_meta_catalog_page(
                    incompatible,
                    "synthetic-token",
                    "act_123",
                    "campaign",
                    reporting_context_hash=self.context_hash,
                )
            request.assert_not_called()

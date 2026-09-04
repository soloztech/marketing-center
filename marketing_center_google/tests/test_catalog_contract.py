import datetime
from types import SimpleNamespace

from odoo.tests.common import TransactionCase

from odoo.addons.google_api_base.services.errors import GoogleApiError
from odoo.addons.marketing_center_base.services.catalog_dto import sha256_text

from ..services.catalog import (
    GOOGLE_CATALOG_CONTRACT_VERSION,
    GOOGLE_CATALOG_ENTITY_TYPES,
    decode_google_catalog_cursor,
    encode_google_catalog_cursor,
    google_catalog_reporting_context,
    google_catalog_spec,
    normalize_google_catalog_page,
)


class TestGoogleCatalogContract(TransactionCase):
    def test_reporting_context_fences_the_v25_2_contract(self):
        context = google_catalog_reporting_context("1234567890")
        self.assertEqual(
            context["contract_version"],
            GOOGLE_CATALOG_CONTRACT_VERSION,
        )
        self.assertEqual(GOOGLE_CATALOG_CONTRACT_VERSION, "google.ads.catalog.v25.2")

    def _page(self, row, *, next_token=""):
        return SimpleNamespace(
            results=(row,),
            next_page_token=next_token,
            request_id="request-catalog",
        )

    def _row(self, stage):
        rows = {
            "campaign": {
                "campaign": {
                    "resourceName": "customers/1234567890/campaigns/10",
                    "id": "10",
                    "name": "Search",
                    "status": "ENABLED",
                    "advertisingChannelType": "SEARCH",
                    "startDateTime": "2026-01-01T00:00:00Z",
                    "endDateTime": "2026-12-31T23:59:59Z",
                }
            },
            "ad_group": {
                "campaign": {"resourceName": "customers/1234567890/campaigns/10"},
                "adGroup": {
                    "resourceName": "customers/1234567890/adGroups/20",
                    "id": "20",
                    "name": "Solar",
                    "status": "ENABLED",
                    "type": "SEARCH_STANDARD",
                },
            },
            "ad": {
                "adGroup": {"resourceName": "customers/1234567890/adGroups/20"},
                "adGroupAd": {
                    "resourceName": "customers/1234567890/adGroupAds/20~30",
                    "status": "ENABLED",
                    "ad": {
                        "id": "30",
                        "name": "Responsive ad",
                        "type": "RESPONSIVE_SEARCH_AD",
                        "finalUrls": ["https://example.invalid/solar"],
                    },
                },
            },
            "asset": {
                "asset": {
                    "resourceName": "customers/1234567890/assets/40",
                    "id": "40",
                    "name": "Headline",
                    "type": "TEXT",
                    "source": "ADVERTISER",
                }
            },
            "keyword": {
                "adGroup": {"resourceName": "customers/1234567890/adGroups/20"},
                "adGroupCriterion": {
                    "resourceName": "customers/1234567890/adGroupCriteria/20~50",
                    "criterionId": "50",
                    "status": "ENABLED",
                    "negative": False,
                    "keyword": {"text": "energia solar", "matchType": "PHRASE"},
                },
            },
            "conversion_action": {
                "conversionAction": {
                    "resourceName": "customers/1234567890/conversionActions/60",
                    "id": "60",
                    "name": "Lead",
                    "status": "ENABLED",
                    "type": "WEBPAGE",
                    "category": "SUBMIT_LEAD_FORM",
                    "primaryForGoal": True,
                    "includeInConversionsMetric": True,
                }
            },
        }
        return rows[stage]

    def test_fixed_v25_catalog_is_normalized_without_raw_payload(self):
        context_hash = sha256_text("google-catalog")
        observed_at = datetime.datetime(2026, 9, 1, 12, 0)
        for index, stage in enumerate(GOOGLE_CATALOG_ENTITY_TYPES):
            with self.subTest(stage=stage):
                pages = normalize_google_catalog_page(
                    "1234567890",
                    stage,
                    self._page(self._row(stage)),
                    reporting_context_hash=context_hash,
                    observed_at=observed_at,
                )
                self.assertEqual(len(pages), 1)
                self.assertEqual(pages[0].items[0].entity_type, stage)
                self.assertEqual(pages[0].items[0].observed_at, observed_at)
                self.assertFalse(pages[0].authoritative_complete)
                self.assertEqual(
                    pages[0].has_more,
                    index < len(GOOGLE_CATALOG_ENTITY_TYPES) - 1,
                )
                if stage == "campaign":
                    self.assertEqual(
                        pages[0].items[0].attributes,
                        {
                            "google.advertisingChannelType": "SEARCH",
                            "google.startDateTime": "2026-01-01T00:00:00Z",
                            "google.endDateTime": "2026-12-31T23:59:59Z",
                        },
                    )

    def test_provider_pagination_stays_in_same_catalog_stage(self):
        pages = normalize_google_catalog_page(
            "1234567890",
            "campaign",
            self._page(self._row("campaign"), next_token="opaque-page-2"),
            reporting_context_hash=sha256_text("google-catalog"),
        )
        self.assertTrue(pages[-1].has_more)
        self.assertEqual(
            decode_google_catalog_cursor(pages[-1].next_cursor),
            ("campaign", "opaque-page-2"),
        )

    def test_provider_pagination_rejects_repeated_token(self):
        with self.assertRaises(GoogleApiError):
            normalize_google_catalog_page(
                "1234567890",
                "campaign",
                self._page(self._row("campaign"), next_token="opaque-page-2"),
                current_page_token="opaque-page-2",
                reporting_context_hash=sha256_text("google-catalog"),
            )

    def test_cursor_and_query_allowlists_reject_untrusted_input(self):
        for stage in GOOGLE_CATALOG_ENTITY_TYPES:
            spec = google_catalog_spec(stage)
            self.assertIn("ORDER BY", spec.query)
            self.assertNotIn("{", spec.query)
            select_clause = spec.query.partition(" FROM ")[0]
            for order_field in spec.query.partition(" ORDER BY ")[2].split(", "):
                self.assertIn(order_field, select_clause)
        campaign_query = google_catalog_spec("campaign").query
        self.assertIn("campaign.start_date_time", campaign_query)
        self.assertIn("campaign.end_date_time", campaign_query)
        self.assertNotIn("campaign.start_date,", campaign_query)
        self.assertNotIn("campaign.end_date ", campaign_query)
        for value in (
            "",
            '{"stage":"campaign","token":"x","version":2}',
            '{"stage":"unknown","token":"x","version":1}',
            "not-json",
        ):
            with self.subTest(value=value), self.assertRaises(GoogleApiError):
                decode_google_catalog_cursor(value or "not-json")
        with self.assertRaises(GoogleApiError):
            encode_google_catalog_cursor("campaign", "bad\nvalue")

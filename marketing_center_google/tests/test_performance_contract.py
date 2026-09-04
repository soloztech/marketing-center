import datetime
from types import SimpleNamespace

from odoo.tests.common import TransactionCase

from odoo.addons.google_api_base.services.errors import GoogleApiError
from odoo.addons.marketing_center_base.services.catalog_dto import sha256_text

from ..services.performance import (
    GOOGLE_PERFORMANCE_GRAINS,
    google_performance_query,
    normalize_google_performance_page,
)


class TestGooglePerformanceContract(TransactionCase):
    _RESOURCE_ROWS = {
        "account": {"customer": {"resourceName": "customers/1234567890"}},
        "campaign": {"campaign": {"resourceName": "customers/1234567890/campaigns/10"}},
        "ad_group": {"adGroup": {"resourceName": "customers/1234567890/adGroups/20"}},
        "ad": {"adGroupAd": {"resourceName": "customers/1234567890/adGroupAds/20~30"}},
        "keyword": {
            "adGroupCriterion": {
                "resourceName": "customers/1234567890/adGroupCriteria/20~40"
            }
        },
    }

    def test_all_canonical_grains_use_fixed_daily_gaql_and_dto(self):
        context_hash = sha256_text("google-performance")
        for grain in GOOGLE_PERFORMANCE_GRAINS:
            with self.subTest(grain=grain):
                query = google_performance_query(
                    grain,
                    datetime.date(2026, 8, 31),
                    datetime.date(2026, 8, 31),
                )
                self.assertIn("segments.date", query)
                self.assertIn("metrics.cost_micros", query)
                row = dict(self._RESOURCE_ROWS[grain])
                row.update(
                    {
                        "segments": {"date": "2026-08-31"},
                        "metrics": {
                            "impressions": "3000000001",
                            "clicks": "42",
                            "costMicros": "9876543210",
                        },
                    }
                )
                pages = normalize_google_performance_page(
                    "1234567890",
                    grain,
                    SimpleNamespace(
                        results=(row,),
                        next_page_token="",
                        request_id="request-performance",
                    ),
                    date_from=datetime.date(2026, 8, 31),
                    date_to=datetime.date(2026, 8, 31),
                    currency="BRL",
                    report_timezone="America/Sao_Paulo",
                    reporting_context_hash=context_hash,
                    observed_at=datetime.datetime(2026, 9, 1, 12, 0),
                )
                metric = pages[0].items[0]
                self.assertEqual(metric.grain, grain)
                self.assertEqual(metric.impressions, 3000000001)
                self.assertEqual(metric.cost_micros, 9876543210)

    def test_performance_window_resource_and_cursor_are_strict(self):
        with self.assertRaises(GoogleApiError):
            google_performance_query(
                "campaign",
                datetime.date(2026, 7, 1),
                datetime.date(2026, 8, 1),
            )
        row = dict(self._RESOURCE_ROWS["campaign"])
        row["campaign"] = {"resourceName": "customers/9999999999/campaigns/10"}
        row.update(
            {
                "segments": {"date": "2026-08-31"},
                "metrics": {"impressions": 1, "clicks": 1, "costMicros": 1},
            }
        )
        with self.assertRaises(GoogleApiError):
            normalize_google_performance_page(
                "1234567890",
                "campaign",
                SimpleNamespace(results=(row,), next_page_token=""),
                date_from=datetime.date(2026, 8, 31),
                date_to=datetime.date(2026, 8, 31),
                currency="BRL",
                report_timezone="UTC",
                reporting_context_hash=sha256_text("context"),
            )

    def test_provider_pagination_rejects_repeated_token(self):
        row = dict(self._RESOURCE_ROWS["account"])
        row.update(
            {
                "segments": {"date": "2026-08-31"},
                "metrics": {"impressions": 1, "clicks": 1, "costMicros": 1},
            }
        )
        with self.assertRaises(GoogleApiError):
            normalize_google_performance_page(
                "1234567890",
                "account",
                SimpleNamespace(results=(row,), next_page_token="same-token"),
                current_page_token="same-token",
                date_from=datetime.date(2026, 8, 31),
                date_to=datetime.date(2026, 8, 31),
                currency="BRL",
                report_timezone="UTC",
                reporting_context_hash=sha256_text("context"),
            )

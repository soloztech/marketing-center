import datetime

from odoo.tests.common import TransactionCase

from ..services.performance_dto import (
    EMPTY_REPORTING_CONTEXT_HASH,
    MarketingPerformanceDTO,
    PerformanceDTOValidationError,
    PerformancePageDTO,
)


class TestMarketingPerformanceDTO(TransactionCase):
    def _dto(self, **overrides):
        values = {
            "grain": "campaign",
            "entity_external_ref": "accounts/A/campaigns/C",
            "report_date": datetime.date(2026, 8, 30),
            "period_start_utc": datetime.datetime(2026, 8, 30, 3, 0),
            "period_end_utc": datetime.datetime(2026, 8, 31, 3, 0),
            "report_timezone": "America/Sao_Paulo",
            "currency": "brl",
            "observed_at": datetime.datetime(2026, 8, 31, 12, 0),
            "impressions": 5_000_000_000,
            "clicks": 42,
            "cost_micros": 123_456_789,
            "reporting_context_hash": EMPTY_REPORTING_CONTEXT_HASH,
        }
        values.update(overrides)
        return MarketingPerformanceDTO(**values)

    def test_bigints_and_wire_roundtrip_are_exact(self):
        dto = self._dto()
        self.assertEqual(dto.impressions, 5_000_000_000)
        self.assertEqual(dto.to_dict()["cost_micros"], 123_456_789)
        self.assertEqual(
            MarketingPerformanceDTO.from_dict(dto.to_dict()).content_hash,
            dto.content_hash,
        )

    def test_missing_and_explicit_zero_are_distinct(self):
        missing = self._dto(impressions=None)
        zero = self._dto(impressions=0)
        self.assertNotEqual(missing.content_hash, zero.content_hash)
        self.assertIsNone(missing.canonical_content()["impressions"])
        self.assertEqual(zero.canonical_content()["impressions"], 0)

    def test_daily_bounds_timezone_currency_and_dimensions_are_strict(self):
        invalid_values = (
            {"grain": "ad"},
            {"currency": "REAL"},
            {"report_timezone": "Invalid/Timezone"},
            {"period_start_utc": datetime.datetime(2026, 8, 30, 0, 0)},
            {"dimensions": {"secret_dimension": "x"}},
            {"impressions": -1},
            {"impressions": True},
        )
        for values in invalid_values:
            with self.subTest(values=values), self.assertRaises(
                PerformanceDTOValidationError
            ):
                self._dto(**values)

    def test_at_least_one_metric_is_required(self):
        with self.assertRaises(PerformanceDTOValidationError):
            self._dto(impressions=None, clicks=None, cost_micros=None)

    def test_page_rejects_duplicate_identity_and_unsafe_errors(self):
        metric = self._dto()
        with self.assertRaises(PerformanceDTOValidationError):
            PerformancePageDTO(items=(metric, metric))
        with self.assertRaises(PerformanceDTOValidationError):
            PerformancePageDTO(errors=({"code": "bad", "access_token": "x"},))

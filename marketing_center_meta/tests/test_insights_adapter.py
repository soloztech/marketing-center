import datetime
from types import SimpleNamespace
from unittest.mock import patch

from odoo.tests.common import SavepointCase

from odoo.addons.meta_api_base.services.errors import MetaApiError

from ..services.insights import (
    fetch_meta_insights_page,
    meta_insights_reporting_context,
    normalize_insights_window,
)


class TestMetaInsightsAdapter(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = SimpleNamespace(graph_version="v26.0")
        cls.observed_at = datetime.datetime(2026, 8, 31, 12, 0)
        cls.context_hash = "a" * 64

    def _fetch(
        self,
        grain,
        payload,
        *,
        after="",
        date_from="2026-08-24",
        date_to="2026-08-30",
        currency="BRL",
        timezone="America/Sao_Paulo",
        app=None,
    ):
        with patch(
            "odoo.addons.marketing_center_meta.services.insights.graph_request",
            return_value=payload,
        ) as request:
            page = fetch_meta_insights_page(
                app or self.app,
                "synthetic-token",
                "act_123",
                grain,
                date_from=date_from,
                date_to=date_to,
                currency=currency,
                report_timezone=timezone,
                after=after,
                reporting_context_hash=self.context_hash,
                observed_at=self.observed_at,
            )
        return page, request

    def test_account_daily_row_uses_exact_fixed_query_and_missing_semantics(self):
        page, request = self._fetch(
            "account",
            {
                "data": [
                    {
                        "account_id": "123",
                        "account_currency": "BRL",
                        "date_start": "2026-08-30",
                        "date_stop": "2026-08-30",
                        "impressions": "0",
                        "spend": "0.000001",
                    }
                ]
            },
        )
        metric = page.items[0]
        self.assertEqual(metric.entity_external_ref, "act_123")
        self.assertEqual(metric.grain, "account")
        self.assertEqual(metric.impressions, 0)
        self.assertIsNone(metric.clicks)
        self.assertEqual(metric.cost_micros, 1)
        self.assertEqual(
            metric.period_start_utc,
            datetime.datetime(2026, 8, 30, 3, 0),
        )
        self.assertEqual(
            metric.period_end_utc,
            datetime.datetime(2026, 8, 31, 3, 0),
        )

        self.assertEqual(request.call_args.args[2], "GET")
        self.assertEqual(request.call_args.args[3], "act_123/insights")
        params = request.call_args.kwargs["params"]
        self.assertEqual(
            params,
            {
                "fields": (
                    "account_id,account_currency,date_start,date_stop,"
                    "impressions,clicks,spend"
                ),
                "level": "account",
                "time_increment": 1,
                "time_range": '{"since":"2026-08-24","until":"2026-08-30"}',
                "limit": 100,
            },
        )
        rendered = str(params).lower()
        for forbidden in (
            "actions",
            "action_values",
            "conversions",
            "breakdowns",
            "attribution",
            "reach",
            "ctr",
            "cpc",
            "cpm",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_campaign_identity_and_bigint_values_remain_exact(self):
        page, request = self._fetch(
            "campaign",
            {
                "data": [
                    {
                        "account_id": "123",
                        "account_currency": "BRL",
                        "campaign_id": "456",
                        "campaign_name": "Historical campaign",
                        "date_start": "2026-08-29",
                        "date_stop": "2026-08-29",
                        "impressions": "3000000001",
                        "clicks": 2147483648,
                        "spend": "123456789.123456",
                        "actions": [{"action_type": "lead", "value": "99"}],
                    }
                ]
            },
        )
        metric = page.items[0]
        self.assertEqual(metric.entity_external_ref, "act_123/campaigns/456")
        self.assertEqual(metric.impressions, 3000000001)
        self.assertEqual(metric.clicks, 2147483648)
        self.assertEqual(metric.cost_micros, 123456789123456)
        fields = request.call_args.kwargs["params"]["fields"]
        self.assertTrue(fields.endswith("campaign_id,campaign_name"))
        self.assertNotIn("actions", fields)

    def test_paging_uses_only_after_cursor_and_never_next_url(self):
        page, request = self._fetch(
            "account",
            {
                "data": [],
                "paging": {
                    "cursors": {"after": "opaque-page-2"},
                    "next": "https://graph.facebook.com/private?access_token=secret",
                },
            },
            after="opaque-page-1",
        )
        self.assertTrue(page.has_more)
        self.assertEqual(page.next_cursor, "opaque-page-2")
        self.assertNotIn("private", page.next_cursor)
        self.assertEqual(
            request.call_args.kwargs["params"]["after"],
            "opaque-page-1",
        )

        terminal, _request = self._fetch(
            "account",
            {"data": [], "paging": {"cursors": {"after": "unused"}}},
        )
        self.assertFalse(terminal.has_more)
        self.assertFalse(terminal.next_cursor)

    def test_context_commits_to_strict_no_action_contract(self):
        context = meta_insights_reporting_context(
            "v26.0",
            "act_123",
            "campaign",
            "BRL",
            "America/Sao_Paulo",
        )
        self.assertEqual(context["actions_policy"], "disabled")
        self.assertEqual(context["attribution_policy"], "disabled")
        self.assertEqual(context["dimensions"], [])
        self.assertEqual(context["breakdowns"], [])
        self.assertEqual(context["filtering"], [])
        self.assertEqual(context["time_increment"], 1)
        self.assertNotIn("actions", context["fields"])
        self.assertNotIn("reach", context["fields"])

    def test_daily_boundaries_support_23_and_25_hour_days(self):
        _start, _end, spring_start, spring_end = normalize_insights_window(
            "2026-03-08",
            "2026-03-08",
            "America/New_York",
        )
        self.assertEqual(
            spring_end - spring_start,
            datetime.timedelta(hours=23),
        )
        _start, _end, fall_start, fall_end = normalize_insights_window(
            "2026-11-01",
            "2026-11-01",
            "America/New_York",
        )
        self.assertEqual(
            fall_end - fall_start,
            datetime.timedelta(hours=25),
        )

    def test_invalid_contract_rows_windows_and_paging_are_rejected(self):
        valid_row = {
            "account_id": "123",
            "account_currency": "BRL",
            "date_start": "2026-08-30",
            "date_stop": "2026-08-30",
            "impressions": "1",
            "clicks": "1",
            "spend": "1.00",
        }
        invalid_rows = (
            {**valid_row, "account_id": "999"},
            {**valid_row, "account_currency": "USD"},
            {**valid_row, "date_stop": "2026-08-29"},
            {**valid_row, "date_start": "2026-08-01", "date_stop": "2026-08-01"},
            {**valid_row, "impressions": "1.5"},
            {**valid_row, "clicks": -1},
            {**valid_row, "spend": "-0.01"},
            {**valid_row, "spend": "0.0000001"},
            {**valid_row, "spend": "1e999999"},
            {
                "account_id": "123",
                "account_currency": "BRL",
                "date_start": "2026-08-30",
                "date_stop": "2026-08-30",
            },
        )
        for row in invalid_rows:
            with self.assertRaises(MetaApiError):
                self._fetch("account", {"data": [row]})

        invalid_pages = (
            {"data": [], "paging": []},
            {"data": [], "paging": {"cursors": []}},
            {"data": [], "paging": {"next": []}},
            {
                "data": [],
                "paging": {"next": "private", "cursors": {"after": "same"}},
            },
        )
        for payload in invalid_pages:
            with self.assertRaises(MetaApiError):
                self._fetch("account", payload, after="same")

        with self.assertRaises(MetaApiError):
            self._fetch(
                "account",
                {"data": []},
                date_from="2026-07-01",
                date_to="2026-08-01",
            )
        with self.assertRaisesRegex(
            MetaApiError,
            "Insights supports Graph v26.0.*configured as v25.0",
        ):
            self._fetch(
                "account",
                {"data": []},
                app=SimpleNamespace(graph_version="v25.0"),
            )

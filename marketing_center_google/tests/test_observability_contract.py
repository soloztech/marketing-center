import datetime
import hashlib
from types import SimpleNamespace

from odoo.tests.common import TransactionCase

from odoo.addons.google_api_base.services.errors import GoogleApiError
from odoo.addons.marketing_center_base.services.catalog_dto import sha256_text

from ..services.observability import (
    GOOGLE_CHANGE_CONTRACT_VERSION,
    GOOGLE_CHANGE_ROW_LIMIT,
    GOOGLE_DIAGNOSTIC_CONTRACT_VERSION,
    GOOGLE_DIAGNOSTIC_STAGES,
    decode_google_diagnostic_cursor,
    google_change_query,
    google_change_reporting_context,
    google_diagnostic_spec,
    normalize_google_change_page,
    normalize_google_diagnostic_page,
)


class TestGoogleObservabilityContract(TransactionCase):
    def _page(self, row, *, next_token=""):
        return SimpleNamespace(
            results=(row,),
            next_page_token=next_token,
            request_id="request-observation",
        )

    def _change_row(self, **values):
        event = {
            "resourceName": "customers/1234567890/changeEvents/1700000000000000~1~0",
            "changeDateTime": "2026-08-31 09:15:30",
            "changeResourceName": "customers/1234567890/campaigns/10",
            "userEmail": "Operator@Example.Invalid",
            "clientType": "GOOGLE_ADS_WEB_CLIENT",
            "changeResourceType": "CAMPAIGN",
            "resourceChangeOperation": "UPDATE",
            "changedFields": "status,name,status",
        }
        event.update(values)
        return {"changeEvent": event}

    def test_change_query_is_finite_and_excludes_resource_snapshots(self):
        query = google_change_query(datetime.date(2026, 8, 31))
        self.assertIn("change_event.change_date_time >= '2026-08-31 00:00:00'", query)
        self.assertIn("change_event.change_date_time < '2026-09-01 00:00:00'", query)
        self.assertIn("LIMIT %s" % GOOGLE_CHANGE_ROW_LIMIT, query)
        self.assertNotIn("old_resource", query)
        self.assertNotIn("new_resource", query)
        context = google_change_reporting_context(
            "1234567890", datetime.date(2026, 8, 31), "America/Sao_Paulo"
        )
        self.assertEqual(context["contract_version"], GOOGLE_CHANGE_CONTRACT_VERSION)
        self.assertEqual(context["limit"], 10_000)

    def test_change_page_hashes_actor_and_keeps_allowlisted_metadata(self):
        page = normalize_google_change_page(
            "1234567890",
            self._page(self._change_row()),
            local_date=datetime.date(2026, 8, 31),
            report_timezone="America/Sao_Paulo",
            reporting_context_hash=sha256_text("change-context"),
            observed_at=datetime.datetime(2026, 9, 1, 12, 0),
        )[0]
        item = page.items[0]
        self.assertEqual(item.occurred_at, datetime.datetime(2026, 8, 31, 12, 15, 30))
        self.assertEqual(item.changed_fields, ("name", "status"))
        self.assertEqual(
            item.actor_hash,
            hashlib.sha256(b"operator@example.invalid").hexdigest(),
        )
        self.assertNotIn("Operator@Example.Invalid", repr(item))
        self.assertEqual(item.resource_type, "campaign")
        self.assertEqual(item.operation, "update")

    def test_change_page_rejects_rows_outside_closed_local_day(self):
        with self.assertRaises(GoogleApiError):
            normalize_google_change_page(
                "1234567890",
                self._page(self._change_row(changeDateTime="2026-09-01 00:00:00")),
                local_date=datetime.date(2026, 8, 31),
                report_timezone="America/Sao_Paulo",
                reporting_context_hash=sha256_text("change-context"),
            )

    def test_change_page_rejects_repeated_provider_cursor(self):
        with self.assertRaises(GoogleApiError):
            normalize_google_change_page(
                "1234567890",
                self._page(self._change_row(), next_token="opaque-2"),
                current_page_token="opaque-2",
                local_date=datetime.date(2026, 8, 31),
                report_timezone="America/Sao_Paulo",
                reporting_context_hash=sha256_text("change-context"),
            )

    def test_diagnostic_contract_minimizes_policy_payload(self):
        row = {
            "adGroupAd": {
                "resourceName": "customers/1234567890/adGroupAds/20~30",
                "status": "ENABLED",
                "primaryStatus": "LIMITED",
                "primaryStatusReasons": ["POLICY_LIMITED", "POLICY_LIMITED"],
                "policySummary": {
                    "approvalStatus": "APPROVED_LIMITED",
                    "reviewStatus": "REVIEWED",
                    "policyTopicEntries": [
                        {
                            "topic": "Restricted medical content",
                            "type": "LIMITED",
                            "evidences": [{"textList": {"texts": ["sensitive"]}}],
                        }
                    ],
                },
            }
        }
        page = normalize_google_diagnostic_page(
            "1234567890",
            "ad",
            self._page(row),
            reporting_context_hash=sha256_text("diagnostic-context"),
            observed_at=datetime.datetime(2026, 9, 1, 12, 0),
        )[0]
        item = page.items[0]
        self.assertEqual(item.severity, "warning")
        self.assertEqual(item.status_reasons, ("policy_limited",))
        self.assertEqual(
            item.policy_topics,
            ({"topic": "Restricted medical content", "type": "limited"},),
        )
        self.assertNotIn("sensitive", repr(item))
        self.assertEqual(
            GOOGLE_DIAGNOSTIC_CONTRACT_VERSION,
            "google.ads.delivery-diagnostics.v25.1",
        )

    def test_diagnostic_cursor_advances_across_a_fixed_stage_allowlist(self):
        for index, stage in enumerate(GOOGLE_DIAGNOSTIC_STAGES):
            spec = google_diagnostic_spec(stage)
            self.assertNotIn("{", spec.query)
            self.assertIn("ORDER BY", spec.query)
            row_key = spec.row_key
            resource = {
                "campaign": "customers/1234567890/campaigns/10",
                "adGroup": "customers/1234567890/adGroups/20",
                "adGroupAd": "customers/1234567890/adGroupAds/20~30",
            }[row_key]
            pages = normalize_google_diagnostic_page(
                "1234567890",
                stage,
                self._page(
                    {
                        row_key: {
                            "resourceName": resource,
                            "status": "ENABLED",
                            "primaryStatus": "ELIGIBLE",
                            "primaryStatusReasons": [],
                        }
                    }
                ),
                reporting_context_hash=sha256_text("diagnostic-context"),
            )
            if index + 1 < len(GOOGLE_DIAGNOSTIC_STAGES):
                self.assertTrue(pages[-1].has_more)
                self.assertEqual(
                    decode_google_diagnostic_cursor(pages[-1].next_cursor),
                    (GOOGLE_DIAGNOSTIC_STAGES[index + 1], ""),
                )
            else:
                self.assertFalse(pages[-1].has_more)
                self.assertFalse(pages[-1].next_cursor)

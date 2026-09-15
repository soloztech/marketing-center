import datetime
import hashlib
import json
import uuid
from unittest import TestCase
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import SavepointCase

from odoo.addons.google_api_base.services.errors import (
    GoogleApiError,
    GoogleApiRateLimitError,
)
from odoo.addons.google_api_base.services.facade import GoogleSearchPage
from odoo.addons.marketing_center_base.services.catalog_dto import ExternalEntityDTO
from odoo.addons.marketing_center_base.services.dto import (
    MarketingIdentifierDTO,
    MarketingTouchpointDTO,
)

from ..services.click import (
    GoogleClickMatch,
    click_local_date,
    click_query,
    normalize_click_page,
)
from .common import create_google_profile, project_google_source

_ADAPTER = (
    "odoo.addons.marketing_center_google.models.click_lookup.GoogleMarketingReadAdapter"
)
_GCLID = "synthetic_google_click_123"
_CUSTOMER = "1234567890"
_CAMPAIGN = "customers/1234567890/campaigns/10"


class TestGoogleClickContract(TestCase):
    def _row(self):
        return {
            "customer": {"id": _CUSTOMER, "timeZone": "America/Sao_Paulo"},
            "segments": {
                "date": "2026-09-14",
                "adNetworkType": "SEARCH",
                "device": "MOBILE",
            },
            "clickView": {
                "gclid": _GCLID,
                "adGroupAd": "customers/1234567890/adGroupAds/20~30",
                "keywordInfo": {"text": "test keyword", "matchType": "EXACT"},
            },
            "campaign": {"id": "10", "resourceName": _CAMPAIGN, "name": "Search test"},
            "adGroup": {
                "id": "20",
                "resourceName": "customers/1234567890/adGroups/20",
                "name": "Group test",
            },
        }

    def _page(self, *rows, **values):
        return GoogleSearchPage.from_values(results=list(rows), **values)

    def _normalize(self, page):
        return normalize_click_page(
            page, _CUSTOMER, _GCLID, datetime.date(2026, 9, 14), "America/Sao_Paulo"
        )

    def test_query_is_exact_single_day_and_rejects_interpolation(self):
        query = click_query(_GCLID, datetime.date(2026, 9, 14))
        self.assertIn("segments.date = '2026-09-14'", query)
        self.assertIn("click_view.gclid = '%s'" % _GCLID, query)
        self.assertTrue(query.endswith("LIMIT 2"))
        for value in ("x' OR 1=1", "x\\y", "x\ny", "", "a" * 2049):
            with self.assertRaises(GoogleApiError) as caught:
                click_query(value, datetime.date(2026, 9, 14))
            self.assertNotIn(
                value or "raw click", str(caught.exception)
            ) if value else None

    def test_date_uses_account_midnight_and_90_day_boundary(self):
        now = datetime.datetime(2026, 9, 15, 2, 0)
        self.assertEqual(
            click_local_date(now, "America/Sao_Paulo", now=now),
            datetime.date(2026, 9, 14),
        )
        self.assertEqual(
            click_local_date(
                now - datetime.timedelta(days=90), "America/Sao_Paulo", now=now
            ),
            datetime.date(2026, 6, 16),
        )
        for instant in (
            now - datetime.timedelta(days=91),
            now + datetime.timedelta(days=1),
        ):
            with self.assertRaises(GoogleApiError):
                click_local_date(instant, "America/Sao_Paulo", now=now)

    def test_normalization_keeps_assets_and_discards_identifier(self):
        match = self._normalize(self._page(self._row()))
        self.assertEqual(match.assets["google.campaign_id"], _CAMPAIGN)
        self.assertEqual(match.details["network"], "SEARCH")
        self.assertNotIn(_GCLID, repr(match))
        self.assertIsNone(self._normalize(self._page()))

    def test_response_requires_exact_identifier_account_date_timezone(self):
        changes = [
            ("clickView", "gclid", "another"),
            ("customer", "id", "9999999999"),
            ("customer", "timeZone", "UTC"),
            ("segments", "date", "2026-09-15"),
            ("campaign", "resourceName", "customers/9999999999/campaigns/10"),
        ]
        for section, key, value in changes:
            row = self._row()
            row[section][key] = value
            with self.assertRaises(GoogleApiError):
                self._normalize(self._page(row))

    def test_ambiguous_or_paginated_response_is_rejected(self):
        for page in (
            self._page(self._row(), self._row()),
            self._page(self._row(), next_page_token="more"),
        ):
            with self.assertRaises(GoogleApiError):
                self._normalize(page)


@tagged("post_install", "-at_install", "native_google_click")
class TestGoogleClickLookup(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.identity, cls.profile = create_google_profile(
            cls.env, name="Google click laboratory"
        )
        _customer, cls.source, cls.connection = project_google_source(
            cls.env, cls.profile, timezone="America/Sao_Paulo"
        )
        cls.source.write({"google_click_lookup_enabled": True})
        cls.service = cls.env["marketing.center.google.click.service"]
        cls.ingestion = cls.env["marketing.attribution.service"]

    def _point(self, namespace="google.gclid", *, occurred_at=None, consent="unknown"):
        from odoo.addons.marketing_center_base.services.dto import PrivacySnapshotDTO

        dto = MarketingTouchpointDTO(
            source_system="web.ingress",
            source_scope_ref="click-test",
            source_occurrence_ref=str(uuid.uuid4()),
            occurred_at=occurred_at or fields.Datetime.now(),
            platform="web",
            channel="website",
            touchpoint_type="entry_point",
            evidence_level="first_party",
            privacy=PrivacySnapshotDTO(consent_state=consent),
            identifiers=(
                MarketingIdentifierDTO(
                    namespace=namespace,
                    role="click",
                    comparison_hash=hashlib.sha256(_GCLID.encode()).hexdigest(),
                    value_ref="web-click:%s" % uuid.uuid4(),
                    purpose="attribution",
                ),
            ),
        )
        result = self.ingestion._ingest_touchpoint(self.env.company, dto)
        point = self.env["marketing.attribution.touchpoint"].browse(
            result.touchpoint_id
        )
        lookup = self.service._lookups().search(
            [("canonical_key", "=", result.canonical_key)]
        )
        return point, lookup

    def _match(self):
        return GoogleClickMatch(
            assets={"google.customer_id": _CUSTOMER, "google.campaign_id": _CAMPAIGN},
            details={
                "campaign_name": "Search test",
                "network": "SEARCH",
                "device": "DESKTOP",
            },
        )

    def _web_point(self):
        if "marketing.web.ingress.service" not in self.env.registry.models:
            self.skipTest("The optional web ingress integration is not installed.")
        endpoint = self.env["marketing.web.ingress.endpoint"].create(
            {
                "name": "Google click integration endpoint",
                "company_id": self.env.company.id,
                "capture_enabled": True,
                "capture_purpose": "web_attribution",
                "privacy_policy_version": "click-test-v1",
                "privacy_notice_version": "click-test-v1",
                "privacy_legal_basis_code": "documented_test_basis",
                "privacy_policy_justification": "Synthetic Google integration policy.",
                "identifier_retention_days": 30,
                "allowed_origins": "https://www.soloz.example",
                "allowed_hosts": "www.soloz.example",
            }
        )
        now = fields.Datetime.now()
        result = self.env["marketing.web.ingress.service"]._ingest_payload(
            endpoint,
            {
                "event_id": str(uuid.uuid4()),
                "event_type": "entry_point",
                "occurred_at": now.isoformat() + "Z",
                "landing_url": "https://www.soloz.example/solar",
                "gclid": _GCLID,
            },
            origin="https://www.soloz.example",
            observed_at=now,
            body_size_bytes=512,
        )
        self.assertEqual(result.disposition, "accepted")
        point = self.env["marketing.attribution.touchpoint"].browse(
            result.touchpoint_id
        )
        lookup = self.service._lookups().search(
            [("canonical_key", "=", point.canonical_key)]
        )
        self.assertEqual(lookup.state, "pending")
        self.assertTrue(lookup.sync_run_id)
        return point, lookup

    def _execute(self, lookup, match, *, before_return=None):
        run = lookup.sync_run_id
        with patch.object(
            type(self.service), "_protected_input", return_value=_GCLID
        ), patch(_ADAPTER) as adapter:

            def response(*args, **kwargs):
                if before_return:
                    before_return()
                if isinstance(match, Exception):
                    raise match
                return match

            adapter.return_value.fetch_click.side_effect = response
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_lookup_google_click(lookup.attempt_count)
        return result, adapter

    def test_exact_match_enriches_ledger_then_catalog_resolves_later(self):
        point, lookup = self._point()
        original_assets = point.asset_refs_json
        result, adapter = self._execute(lookup, self._match())
        self.assertEqual(result, {"state": "found"})
        self.assertEqual(lookup.sync_run_id.state, "succeeded")
        self.assertEqual(point.asset_refs_json, original_assets)
        enriched = lookup.enriched_touchpoint_id
        self.assertNotEqual(enriched, point)
        self.assertEqual(enriched.canonical_key, point.canonical_key)
        self.assertEqual(enriched.revision_kind, "enrichment")
        self.assertEqual(
            enriched.identifier_ids.value_ref, point.identifier_ids.value_ref
        )
        self.assertNotIn(_GCLID, json.dumps(lookup.result_json))
        resolutions = self.env["marketing.attribution.asset.resolution"].search(
            [
                ("canonical_key", "=", point.canonical_key),
                ("asset_namespace", "=", "google.campaign_id"),
            ]
        )
        self.assertEqual(resolutions.state, "unresolved")
        self.env["marketing.center.catalog.service"]._upsert_entity(
            self.env.company,
            self.source,
            ExternalEntityDTO(
                entity_type="campaign",
                external_ref=_CAMPAIGN,
                external_id="10",
                name="Search test",
                remote_status="enabled",
                observed_at=fields.Datetime.now(),
            ),
        )
        self.assertEqual(resolutions.state, "resolved")
        self.assertEqual(resolutions.entity_id.external_ref, _CAMPAIGN)
        before = enriched.id
        self.service._reconcile_touchpoints([point.id])
        self.assertEqual(lookup.enriched_touchpoint_id.id, before)
        self.assertEqual(adapter.return_value.fetch_click.call_count, 1)

    def test_queue_contains_references_not_click_identifier(self):
        _point, lookup = self._point()
        job = self.env["queue.job"].search(
            [("uuid", "=", lookup.sync_run_id.queue_job_uuid)]
        )
        self.assertNotIn(
            _GCLID, str(job.args) + str(job.kwargs) + job.name + job.identity_key
        )

    def test_real_ingress_exact_match_catalog_updates_existing_crm_lead(self):
        if "marketing.crm.native.utm.service" not in self.env.registry.models:
            self.skipTest("The optional native CRM classifier is not installed.")
        utm_source = self.env["utm.source"].create({"name": "Google click integration"})
        utm_medium = self.env["utm.medium"].create(
            {"name": "Google paid click integration"}
        )
        self.source.write(
            {
                "native_utm_mode": "apply",
                "native_utm_source_id": utm_source.id,
                "native_utm_medium_id": utm_medium.id,
            }
        )
        point, lookup = self._web_point()
        lead = self.env["crm.lead"].create(
            {
                "name": "Existing Google acquisition lead",
                "company_id": self.env.company.id,
                "type": "lead",
                "source_id": False,
                "medium_id": False,
                "campaign_id": False,
                "team_id": False,
                "user_id": False,
            }
        )
        original_lead_id = lead.id
        self.env["marketing.crm.service"]._link_touchpoint_lead(
            point,
            lead,
            source_ref="google-click-integration",
        )
        self.assertEqual(lead._job_resolve_native_utm()["state"], "missing")
        run = lookup.sync_run_id
        with patch(_ADAPTER) as adapter:
            adapter.return_value.fetch_click.return_value = self._match()
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_lookup_google_click(0)
        self.assertEqual(result, {"state": "found"})
        adapter.return_value.fetch_click.assert_called_once()
        call = adapter.return_value.fetch_click.call_args
        self.assertEqual(call.args, (_CUSTOMER, _GCLID))
        self.assertEqual(call.kwargs["occurred_at"], point.occurred_at)
        self.assertEqual(call.kwargs["report_timezone"], self.source.timezone)
        self.assertEqual(lead._job_resolve_native_utm()["state"], "missing")
        self.assertFalse(any(lead._native_utm_values().values()))
        resolutions = self.env["marketing.attribution.asset.resolution"].search(
            [
                ("canonical_key", "=", point.canonical_key),
                ("asset_namespace", "=", "google.campaign_id"),
            ]
        )
        self.assertEqual(resolutions.state, "unresolved")
        result = self.env["marketing.center.catalog.service"]._upsert_entity(
            self.env.company,
            self.source,
            ExternalEntityDTO(
                entity_type="campaign",
                external_ref=_CAMPAIGN,
                external_id="10",
                name="Search test",
                remote_status="enabled",
                observed_at=fields.Datetime.now(),
            ),
        )
        entity = self.env["marketing.center.external.entity"].browse(result.entity_id)
        self.assertEqual(resolutions.state, "resolved")
        self.assertEqual(resolutions.source_id, self.source)
        self.assertEqual(resolutions.entity_id, entity)
        self.assertFalse(entity.native_utm_campaign_id)
        self.assertEqual(lead._job_resolve_native_utm()["state"], "applied")
        self.assertTrue(entity.native_utm_campaign_id)
        self.assertEqual(entity.native_utm_mapping_origin, "automatic")
        self.assertEqual(lead.campaign_id, entity.native_utm_campaign_id)
        self.assertEqual(lead.source_id, utm_source)
        self.assertEqual(lead.medium_id, utm_medium)
        self.assertEqual(lead.id, original_lead_id)
        self.assertFalse(lead.user_id)
        self.assertFalse(lead.team_id)
        self.assertFalse(lead.marketing_utm_manual)
        self.assertEqual(lead.type, "lead")
        receipt = lead.marketing_utm_receipt_id
        campaign = entity.native_utm_campaign_id
        lead._job_resolve_native_utm()
        self.assertEqual(entity.native_utm_campaign_id, campaign)
        self.assertEqual(lead.marketing_utm_receipt_id, receipt)

    def test_real_ingress_quota_retry_preserves_click_attempt_and_job_signature(self):
        _point, lookup = self._web_point()
        run = lookup.sync_run_id
        original_job_uuid = run.queue_job_uuid
        with patch(_ADAPTER) as adapter:
            adapter.return_value.fetch_click.side_effect = GoogleApiRateLimitError(
                "Synthetic private quota " + _GCLID,
                retry_after_seconds=137,
                provider_status="RESOURCE_EXHAUSTED",
            )
            result = run.with_context(
                job_uuid=original_job_uuid
            )._job_lookup_google_click(0)
        self.assertEqual(
            result, {"rescheduled": True, "retry_after": 137, "quota_attempt": 1}
        )
        self.assertEqual(lookup.attempt_count, 0)
        self.assertEqual(lookup.state, "pending")
        self.assertFalse(lookup.enriched_touchpoint_id)
        self.assertNotEqual(run.queue_job_uuid, original_job_uuid)
        self.assertEqual(run.state, "queued")
        self.assertTrue(run.deferred_until)
        self.assertTrue(lookup.next_attempt_at)
        self.assertEqual(self.connection.last_health_error_class, "rate_limited")
        self.assertTrue(self.connection.cooldown_until)
        job = self.env["queue.job"].search([("uuid", "=", run.queue_job_uuid)])
        self.assertTrue(job.eta)
        self.assertEqual(job.method_name, "_job_lookup_google_click")
        self.assertEqual(tuple(job.args), (0, 1))
        self.assertFalse(job.kwargs)
        self.assertNotIn(
            _GCLID, str(job.args) + str(job.kwargs) + job.name + job.identity_key
        )
        self.assertNotIn(_GCLID, run.error_summary or "")

    def test_empty_results_retry_then_finish_unknown(self):
        _point, lookup = self._point()
        for _attempt in range(4):
            result, _adapter = self._execute(lookup, None)
            self.assertEqual(result, {"waiting": True})
            self.assertTrue(lookup.next_attempt_at)
        result, _adapter = self._execute(lookup, None)
        self.assertEqual(result, {"state": "no_match"})
        self.assertEqual(lookup.attempt_count, 5)
        self.assertFalse(lookup.enriched_touchpoint_id)

    def test_delayed_response_can_match_on_retry(self):
        _point, lookup = self._point()
        self._execute(lookup, None)
        self._execute(lookup, self._match())
        self.assertEqual(lookup.state, "found")
        self.assertEqual(lookup.attempt_count, 2)

    def test_account_ambiguity_is_visible_and_prevents_query(self):
        _c, another, _connection = project_google_source(
            self.env, self.profile, customer_id="9999999999"
        )
        another.write({"google_click_lookup_enabled": True})
        _point, lookup = self._point()
        self.assertEqual(lookup.state, "ambiguous")
        self.assertFalse(lookup.sync_run_id)

    def test_disabled_source_and_braid_do_not_schedule(self):
        _point, lookup = self._point(namespace="google.gbraid")
        self.assertFalse(lookup)
        self.source.write({"google_click_lookup_enabled": False})
        _point, lookup = self._point()
        self.assertFalse(lookup)

    def test_source_change_before_job_stops_provider_query(self):
        _point, lookup = self._point()
        self.source.write({"google_click_lookup_enabled": False})
        result, adapter = self._execute(lookup, self._match())
        self.assertEqual(lookup.state, "stale")
        adapter.assert_not_called()

    def test_new_ambiguous_source_before_job_stops_provider_query(self):
        _point, lookup = self._point()
        _c, another, _connection = project_google_source(
            self.env, self.profile, customer_id="9999999999"
        )
        another.write({"google_click_lookup_enabled": True})
        result, adapter = self._execute(lookup, self._match())
        self.assertEqual(result, {"state": "ambiguous"})
        adapter.assert_not_called()

    def test_revoked_input_blocks_query_and_keeps_ledger(self):
        point, lookup = self._point()
        run = lookup.sync_run_id
        with patch.object(
            type(self.service), "_protected_input", return_value=None
        ), patch(_ADAPTER) as adapter:
            result = run.with_context(
                job_uuid=run.queue_job_uuid
            )._job_lookup_google_click(0)
        self.assertEqual(result, {"state": "blocked"})
        adapter.assert_not_called()
        self.assertFalse(lookup.enriched_touchpoint_id)
        self.assertFalse(point.privacy_erased_at)

    def test_old_or_denied_evidence_never_queries(self):
        _point, lookup = self._point(
            occurred_at=fields.Datetime.now() - datetime.timedelta(days=91)
        )
        self.assertEqual(lookup.state, "expired")
        self.assertFalse(lookup.sync_run_id)
        _point, lookup = self._point(consent="denied")
        self.assertFalse(lookup)

    def test_provider_error_details_never_persist(self):
        _point, lookup = self._point()
        self._execute(lookup, GoogleApiError("private query " + _GCLID))
        self.assertEqual(lookup.state, "error")
        self.assertNotIn(_GCLID, lookup.sync_run_id.error_summary or "")
        self.assertFalse(lookup.enriched_touchpoint_id)

    def test_erasure_clears_protected_result_and_blocks_replay(self):
        from odoo.addons.marketing_center_base.models.attribution import (
            ATTRIBUTION_ERASURE_TOKEN,
        )

        point, lookup = self._point()
        self._execute(lookup, self._match())
        point._erase_private_values(
            token=ATTRIBUTION_ERASURE_TOKEN, now=fields.Datetime.now()
        )
        self.assertFalse(lookup.result_json)
        self.assertEqual(lookup.state, "blocked")
        self.assertTrue(lookup.enriched_touchpoint_id.privacy_erased_at)
        self.assertEqual(self.service._reconcile_touchpoints([point.id]), [])

    def test_direct_writes_are_rejected(self):
        _point, lookup = self._point()
        with self.assertRaises(AccessError):
            lookup.with_context(marketing_google_click_token=None).write(
                {"state": "found"}
            )

    def test_cross_company_explicit_reconciliation_rejected(self):
        point, _lookup = self._point()
        other = self.env["res.company"].create({"name": "Other click company"})
        with self.assertRaises(AccessError):
            self.service.with_context(
                allowed_company_ids=[other.id]
            )._reconcile_touchpoints([point.id])

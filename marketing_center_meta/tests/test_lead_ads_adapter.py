import datetime
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from odoo.tests.common import SavepointCase

from odoo.addons.meta_api_base.services.credentials import MetaRuntimeApp
from odoo.addons.meta_api_base.services.errors import MetaApiError

from ..services.adapter import MetaMarketingReadAdapter
from ..services.lead_ads import fetch_meta_lead, fetch_meta_lead_page


class TestMetaLeadAdsAdapter(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.runtime_app = MetaRuntimeApp(
            active=True,
            external_app_id="123456789",
            graph_version="v26.0",
            app_secret="synthetic-meta-app-secret",
            public_ref="4f99eec2-786f-4f9a-a174-b80c752dd9d2",
            revision=7,
            company_id=cls.env.company.id,
        )

    def _payload(self, **overrides):
        payload = {
            "id": "200000000000001",
            "created_time": "2026-09-01T10:30:00+0000",
            "ad_id": "400000000000001",
            "campaign_id": "600000000000001",
            "form_id": "300000000000001",
            "field_data": [
                {"name": "email", "values": ["person@example.com"]},
                {"name": "phone_number", "values": ["+55 19 99999-0000"]},
            ],
        }
        payload.update(overrides)
        return payload

    def test_single_lead_uses_authenticated_graph_v26_contract(self):
        with patch(
            "odoo.addons.marketing_center_meta.services.lead_ads.graph_request",
            return_value=self._payload(),
        ) as request:
            lead = fetch_meta_lead(
                self.runtime_app, "synthetic-token", "200000000000001"
            )
        self.assertEqual(lead.form_id, "300000000000001")
        self.assertEqual(lead.created_at, datetime.datetime(2026, 9, 1, 10, 30))
        self.assertEqual(len(lead.fields), 2)
        self.assertEqual(len(lead.payload_sha256), 64)
        args, kwargs = request.call_args
        self.assertEqual(args[2:4], ("GET", "200000000000001"))
        self.assertEqual(
            kwargs["params"]["fields"],
            "created_time,id,ad_id,campaign_id,form_id,field_data",
        )
        self.assertNotIn("access_token", kwargs["params"])

    def test_pull_is_one_bounded_page_with_explicit_time_filter(self):
        payload = {
            "data": [self._payload()],
            "paging": {
                "next": "https://graph.facebook.com/private",
                "cursors": {"after": "next-cursor"},
            },
        }
        since = datetime.datetime(2026, 8, 31, 10, 30)
        with patch(
            "odoo.addons.marketing_center_meta.services.lead_ads.graph_request",
            return_value=payload,
        ) as request:
            page = fetch_meta_lead_page(
                self.runtime_app,
                "synthetic-token",
                "300000000000001",
                since=since,
            )
        self.assertTrue(page.has_more)
        self.assertEqual(page.next_after, "next-cursor")
        params = request.call_args.kwargs["params"]
        self.assertEqual(params["limit"], 100)
        filtering = json.loads(params["filtering"])
        self.assertEqual(filtering[0]["field"], "time_created")
        self.assertEqual(filtering[0]["operator"], "GREATER_THAN_OR_EQUAL")

    def test_parser_rejects_route_conflict_duplicate_fields_and_cursor_loop(self):
        duplicate = self._payload(
            field_data=[
                {"name": "email", "values": ["a@example.com"]},
                {"name": "email", "values": ["b@example.com"]},
            ]
        )
        invalid_page = {"data": [duplicate], "paging": {}}
        with patch(
            "odoo.addons.marketing_center_meta.services.lead_ads.graph_request",
            return_value=invalid_page,
        ), self.assertRaises(MetaApiError):
            fetch_meta_lead_page(
                self.runtime_app,
                "synthetic-token",
                "300000000000001",
                since=datetime.datetime(2026, 9, 1),
            )

        looping = {
            "data": [self._payload()],
            "paging": {
                "next": "https://graph.facebook.com/private",
                "cursors": {"after": "same"},
            },
        }
        with patch(
            "odoo.addons.marketing_center_meta.services.lead_ads.graph_request",
            return_value=looping,
        ), self.assertRaises(MetaApiError):
            fetch_meta_lead_page(
                self.runtime_app,
                "synthetic-token",
                "300000000000001",
                since=datetime.datetime(2026, 9, 1),
                after="same",
            )

    def test_lead_reader_validation_does_not_require_ads_read(self):
        app = Mock()
        app.with_context.return_value = app
        app._resolve_runtime.return_value = self.runtime_app
        required = (
            "ads_management",
            "leads_retrieval",
            "pages_manage_ads",
            "pages_manage_metadata",
            "pages_read_engagement",
            "pages_show_list",
        )
        profile = SimpleNamespace(
            meta_app_id=app,
            credential_backend="environment",
            access_token_ref="ODOO_META_LEADS_TOKEN",
            reader_kind="lead_reader",
            required_scope_keys=lambda: required,
            ensure_one=lambda: True,
        )
        payload = {
            "data": {
                "app_id": "123456789",
                "is_valid": True,
                "type": "SYSTEM_USER",
                "expires_at": 0,
                "data_access_expires_at": 0,
                "scopes": list(required),
            }
        }
        with patch(
            "odoo.addons.marketing_center_meta.services.adapter.resolve_secret",
            return_value="synthetic-token",
        ), patch(
            "odoo.addons.marketing_center_meta.services.adapter.graph_debug_token",
            return_value=payload,
        ):
            result = MetaMarketingReadAdapter(
                profile, expected_app_revision=7
            ).validate()
        self.assertTrue(result.capabilities["receive_leads"])
        self.assertFalse(result.capabilities["read_entities"])

    def test_lead_contract_fails_closed_outside_graph_v26(self):
        incompatible = SimpleNamespace(graph_version="v25.0")
        with self.assertRaisesRegex(
            MetaApiError,
            "Lead Ads supports Graph v26.0.*configured as v25.0",
        ):
            fetch_meta_lead(incompatible, "synthetic-token", "200000000000001")

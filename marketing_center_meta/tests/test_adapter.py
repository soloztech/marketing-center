from types import SimpleNamespace
from unittest.mock import patch

from odoo.tests.common import SavepointCase

from odoo.addons.meta_api_base.services.errors import MetaApiError, MetaApiPausedError

from ..services.adapter import MetaMarketingReadAdapter
from ..services.credentials import MetaResolvedCredentials


class TestMetaMarketingReadAdapter(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.profile = SimpleNamespace(
            active=True,
            external_app_id="123456789",
            graph_version="v26.0",
            required_scope_keys=lambda: ("ads_read",),
            ensure_one=lambda: True,
        )
        cls.credentials = MetaResolvedCredentials(
            app_secret="synthetic-meta-app-secret",
            access_token="synthetic-meta-reader-token",
        )

    def _adapter(self):
        patcher = patch(
            "odoo.addons.marketing_center_meta.services.adapter.resolve_profile_credentials",
            return_value=self.credentials,
        )
        self.addCleanup(patcher.stop)
        patcher.start()
        return MetaMarketingReadAdapter(self.profile)

    def test_validation_proves_app_scope_and_read_capabilities(self):
        payload = {
            "data": {
                "app_id": "123456789",
                "is_valid": True,
                "type": "SYSTEM_USER",
                "expires_at": 0,
                "data_access_expires_at": 0,
                "scopes": ["ads_read", "leads_retrieval"],
            }
        }
        with patch(
            "odoo.addons.marketing_center_meta.services.adapter.graph_debug_token",
            return_value=payload,
        ):
            result = self._adapter().validate()
        self.assertTrue(result.capabilities["read_entities"])
        self.assertTrue(result.capabilities["read_metrics"])
        self.assertTrue(result.capabilities["receive_leads"])
        self.assertEqual(result.scopes, ("ads_read", "leads_retrieval"))

    def test_validation_rejects_wrong_app_and_missing_scope(self):
        base = {
            "is_valid": True,
            "type": "SYSTEM_USER",
            "expires_at": 0,
            "data_access_expires_at": 0,
            "scopes": ["ads_read"],
        }
        for override in (
            {"app_id": "987654321"},
            {"app_id": "123456789", "scopes": ["business_management"]},
        ):
            with patch(
                "odoo.addons.marketing_center_meta.services.adapter.graph_debug_token",
                return_value={"data": dict(base, **override)},
            ), self.assertRaises(MetaApiPausedError):
                self._adapter().validate()

    def test_discovery_is_paginated_bounded_and_deduplicated(self):
        first = {
            "data": [
                {
                    "id": "act_123",
                    "account_id": "123",
                    "name": "Soloz Meta",
                    "currency": "BRL",
                    "timezone_name": "America/Sao_Paulo",
                    "account_status": 1,
                    "disable_reason": 0,
                }
            ],
            "paging": {
                "cursors": {"after": "next-page"},
                "next": "https://graph.facebook.com/private-next-url",
            },
        }
        second = {"data": [], "paging": {}}
        with patch(
            "odoo.addons.marketing_center_meta.services.adapter.graph_request",
            side_effect=[first, second],
        ) as request:
            accounts = self._adapter().discover_ad_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].external_ref, "act_123")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            request.call_args_list[1].kwargs["params"]["after"], "next-page"
        )

    def test_last_page_cursor_without_next_does_not_trigger_another_request(self):
        payload = {
            "data": [],
            "paging": {"cursors": {"after": "last-page-cursor"}},
        }
        with patch(
            "odoo.addons.marketing_center_meta.services.adapter.graph_request",
            return_value=payload,
        ) as request:
            self.assertEqual(self._adapter().discover_ad_accounts(), ())
        request.assert_called_once()

    def test_repeated_pagination_cursor_is_rejected(self):
        payload = {
            "data": [],
            "paging": {
                "cursors": {"after": "repeated-cursor"},
                "next": "https://graph.facebook.com/private-next-url",
            },
        }
        with patch(
            "odoo.addons.marketing_center_meta.services.adapter.graph_request",
            side_effect=[payload, payload],
        ), self.assertRaises(MetaApiError):
            self._adapter().discover_ad_accounts()

    def test_malformed_paging_and_cursors_are_rejected(self):
        for paging in (["not-an-object"], {"cursors": ["not-an-object"]}):
            with patch(
                "odoo.addons.marketing_center_meta.services.adapter.graph_request",
                return_value={"data": [], "paging": paging},
            ), self.assertRaises(MetaApiError):
                self._adapter().discover_ad_accounts()

    def test_discovery_rejects_inconsistent_account_identity(self):
        payload = {
            "data": [
                {
                    "id": "act_123",
                    "account_id": "456",
                    "name": "Invalid",
                    "currency": "BRL",
                    "timezone_name": "UTC",
                    "account_status": 1,
                    "disable_reason": 0,
                }
            ]
        }
        with patch(
            "odoo.addons.marketing_center_meta.services.adapter.graph_request",
            return_value=payload,
        ), self.assertRaises(MetaApiError):
            self._adapter().discover_ad_accounts()

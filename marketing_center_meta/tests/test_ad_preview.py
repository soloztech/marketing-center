import datetime
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from psycopg2.errors import SerializationFailure

from odoo.tests import tagged
from odoo.tests.common import SavepointCase

from odoo.addons.marketing_center_base.services.catalog_dto import ExternalEntityDTO
from odoo.addons.marketing_center_base.services.tokens import (
    MARKETING_CONFIGURATION_RUNTIME_TOKEN,
)
from odoo.addons.meta_api_base.services.errors import MetaApiError

from ..services.ad_preview import fetch_meta_ad_preview
from ..services.adapter import MetaAdAccount, MetaReadValidation
from ..services.tokens import MARKETING_META_PROFILE_RUNTIME_TOKEN
from .common import create_meta_profile

_GRAPH = "odoo.addons.marketing_center_meta.services.ad_preview.graph_request"
_ADAPTER = (
    "odoo.addons.marketing_center_meta.models.ad_preview.MetaMarketingReadAdapter"
)


@tagged("post_install", "-at_install", "marketing_ad_preview")
class TestMetaAdPreviewAdapter(SavepointCase):
    def _fetch(self, payload):
        with patch(_GRAPH, return_value=payload) as graph:
            result = fetch_meta_ad_preview(
                SimpleNamespace(graph_version="v26.0"),
                "synthetic-token",
                "act_123",
                "act_123/ads/30",
            )
        return result, graph

    def _payload(self, **creative):
        return {
            "id": "30",
            "account_id": "123",
            "creative": {"id": "40", "account_id": "123", **creative},
        }

    def test_single_bounded_read_extracts_actual_creative_copy(self):
        result, graph = self._fetch(
            self._payload(
                title="Motor industrial",
                body="Conheça o motor de 220 volts.",
                thumbnail_url="https://scontent.example/image.jpg?signature=private",
                instagram_permalink_url="https://www.instagram.com/p/example/?tracking=private",
            )
        )
        self.assertEqual(result["title"], "Motor industrial")
        self.assertEqual(result["body"], "Conheça o motor de 220 volts.")
        self.assertEqual(result["public_url"], "https://www.instagram.com/p/example/")
        self.assertEqual(
            result["thumbnail_url"],
            "https://scontent.example/image.jpg?signature=private",
        )
        self.assertEqual(result["media_type"], "image")
        graph.assert_called_once()
        self.assertEqual(graph.call_args.args[2:], ("GET", "30"))
        self.assertEqual(graph.call_args.kwargs["max_response_bytes"], 128 * 1024)
        self.assertIn("creative{", graph.call_args.kwargs["params"]["fields"])
        self.assertNotIn("access_token", graph.call_args.kwargs["params"])

    def test_story_spec_supports_link_and_video_content(self):
        result, _graph = self._fetch(
            self._payload(
                object_story_spec={
                    "link_data": {
                        "name": "Peças industriais",
                        "message": "Solicite um orçamento",
                        "link": "https://example.com/pecas?fbclid=private",
                        "picture": "https://cdn.example/pecas.jpg",
                    }
                }
            )
        )
        self.assertEqual(result["title"], "Peças industriais")
        self.assertEqual(result["public_url"], "https://example.com/pecas")
        result, _graph = self._fetch(
            self._payload(
                video_id="50",
                object_story_spec={
                    "video_data": {
                        "title": "Vídeo",
                        "message": "Veja o motor",
                        "image_url": "https://cdn.example/video.jpg",
                        "call_to_action": {
                            "value": {"link": "https://example.com/video"}
                        },
                    }
                },
            )
        )
        self.assertEqual(result["media_type"], "video")
        self.assertEqual(result["body"], "Veja o motor")

    def test_internal_names_and_dynamic_variants_are_not_ad_copy(self):
        result, _graph = self._fetch(
            self._payload(
                name="Internal creative label",
                object_story_spec={
                    "link_data": {"child_attachments": [{"name": "Unselected variant"}]}
                },
            )
        )
        self.assertEqual(result, {})

    def test_request_scope_is_rejected_before_graph(self):
        with patch(_GRAPH) as graph:
            for account, ref in [
                ("act_999", "act_123/ads/30"),
                ("act_123", "30"),
                ("act_123", "act_123/creatives/40"),
            ]:
                with self.subTest(ref=ref), self.assertRaises(MetaApiError):
                    fetch_meta_ad_preview(
                        SimpleNamespace(graph_version="v26.0"),
                        "synthetic-token",
                        account,
                        ref,
                    )
            graph.assert_not_called()

    def test_response_ad_and_creative_must_match_authorized_account(self):
        for payload in [
            {"id": "31", "account_id": "123"},
            {"id": "30", "account_id": "999"},
            self._payload(account_id="999"),
        ]:
            with self.subTest(payload=payload), self.assertRaises(
                MetaApiError
            ) as caught:
                self._fetch(payload)
            self.assertNotIn("synthetic-token", str(caught.exception))

    def test_output_is_bounded_and_rejects_private_or_malformed_links(self):
        result, _graph = self._fetch(
            self._payload(
                title="x" * 400,
                body="y" * 6000,
                object_url="https://user:secret@example.com/private",
                thumbnail_url="http://cdn.example/image.jpg",
            )
        )
        self.assertEqual(len(result["title"]), 256)
        self.assertEqual(len(result["body"]), 2000)
        self.assertNotIn("public_url", result)
        self.assertNotIn("thumbnail_url", result)


@tagged("post_install", "-at_install", "marketing_ad_preview")
class TestMetaAdPreviewService(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app, cls.profile = create_meta_profile(
            cls.env,
            name="Ad preview fixture",
            external_app_id="123456789",
            app_secret_ref="ODOO_META_PREVIEW_APP_SECRET",
            access_token_ref="ODOO_META_PREVIEW_READER_TOKEN",
        )
        cls.capabilities = {
            "read_entities": True,
            "read_metrics": True,
            "receive_leads": False,
        }
        meta = cls.env["marketing.center.meta.service"]
        meta._mark_profile_success(
            cls.profile,
            MetaReadValidation(
                token_type="system_user",
                expires_at=0,
                data_access_expires_at=0,
                scopes=("ads_read",),
                capabilities=cls.capabilities,
            ),
        )
        cls.source = meta._upsert_source(
            cls.profile,
            cls.capabilities,
            MetaAdAccount(
                external_ref="act_123",
                external_id="123",
                name="Preview account",
                currency=cls.env.company.currency_id.name,
                timezone="UTC",
                account_status=1,
                disable_reason=0,
            ),
        )
        cls.connection = cls.env["marketing.center.connection"].search(
            [("source_id", "=", cls.source.id)]
        )
        ingested = cls.env["marketing.center.catalog.service"]._upsert_entity(
            cls.env.company,
            cls.source,
            ExternalEntityDTO(
                entity_type="ad",
                external_ref="act_123/ads/30",
                external_id="30",
                name="Administrative label",
                observed_at=datetime.datetime(2026, 9, 12, 12),
            ),
        )
        cls.entity = cls.env["marketing.center.external.entity"].browse(
            ingested.entity_id
        )
        cls.service = cls.env["marketing.center.meta.ad.preview.service"]

    def setUp(self):
        super().setUp()
        patcher = patch(_ADAPTER)
        self.adapter = patcher.start()
        self.addCleanup(patcher.stop)
        self.adapter.return_value.fetch_ad_preview.return_value = {
            "title": "Public ad title"
        }
        network = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Real HTTP forbidden"),
        )
        network.start()
        self.addCleanup(network.stop)

    def test_unique_authorized_reader_performs_point_lookup(self):
        self.assertEqual(
            self.service._fetch_ad_preview(self.entity), {"title": "Public ad title"}
        )
        self.adapter.assert_called_once_with(
            self.profile, expected_app_revision=self.app.revision
        )
        self.adapter.return_value.fetch_ad_preview.assert_called_once_with(
            "act_123", "act_123/ads/30"
        )

    def test_disabled_read_scope_stops_before_resolving_credentials(self):
        self.source.write({"read_enabled": False})
        self.assertEqual(self.service._fetch_ad_preview(self.entity), {})
        self.adapter.assert_not_called()

    def test_missing_verified_ads_read_scope_stops_before_http(self):
        self.profile.with_context(
            marketing_meta_profile_runtime_token=MARKETING_META_PROFILE_RUNTIME_TOKEN
        ).write(
            {
                "verified_scopes_json": ["business_management"],
            }
        )
        self.assertEqual(self.service._fetch_ad_preview(self.entity), {})
        self.adapter.assert_not_called()

    def test_missing_read_entities_capability_stops_before_http(self):
        self.connection.with_context(
            marketing_configuration_runtime_token=MARKETING_CONFIGURATION_RUNTIME_TOKEN
        ).write(
            {
                "effective_capabilities_json": {"read_entities": False},
                "effective_capabilities_hash": hashlib.sha256(
                    b'{"read_entities":false}'
                ).hexdigest(),
            }
        )
        self.assertEqual(self.service._fetch_ad_preview(self.entity), {})
        self.adapter.assert_not_called()

    def test_profile_revision_change_stops_before_http(self):
        self.profile.write({"access_token_ref": "ODOO_META_PREVIEW_REPLACEMENT_TOKEN"})
        self.assertEqual(self.service._fetch_ad_preview(self.entity), {})
        self.adapter.assert_not_called()

    def test_revocation_during_http_discards_response(self):
        def revoke(*args):
            self.source.write({"read_enabled": False})
            return {"title": "Must not be stored"}

        self.adapter.return_value.fetch_ad_preview.side_effect = revoke
        self.assertEqual(self.service._fetch_ad_preview(self.entity), {})

    def test_provider_failure_does_not_escape_private_error(self):
        self.adapter.return_value.fetch_ad_preview.side_effect = RuntimeError(
            "secret-token https://private.example"
        )
        self.assertEqual(self.service._fetch_ad_preview(self.entity), {})

    def test_company_outside_active_scope_stops_before_http(self):
        company = self.env["res.company"].create({"name": "Other preview company"})
        service = self.service.with_context(
            allowed_company_ids=[company.id]
        ).with_company(company)
        self.assertEqual(service._fetch_ad_preview(self.entity), {})
        self.adapter.assert_not_called()

    def test_authorization_snapshot_is_memory_only_and_checked_at_final_stage(self):
        result = self.service._fetch_ad_preview(self.entity)
        self.assertTrue(result.fingerprint)
        self.assertEqual(json.loads(json.dumps(result)), {"title": "Public ad title"})
        self.assertTrue(
            self.service._validate_ad_preview(self.entity, result.fingerprint)
        )
        self.source.write({"read_enabled": False})
        self.assertFalse(
            self.service._validate_ad_preview(self.entity, result.fingerprint)
        )

    def test_database_errors_are_not_swallowed_as_empty_enrichment(self):
        self.adapter.return_value.fetch_ad_preview.side_effect = SerializationFailure(
            "scope changed"
        )
        with self.assertRaises(SerializationFailure):
            self.service._fetch_ad_preview(self.entity)

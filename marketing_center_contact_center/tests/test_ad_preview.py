import datetime
import hashlib
import uuid
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import SavepointCase

from odoo.addons.contact_center_base.services.tokens import (
    CONTACT_CENTER_ATTRIBUTION_TOKEN,
)
from odoo.addons.marketing_center_base.services.catalog_dto import ExternalEntityDTO

from . import test_attribution_bridge as bridge_fixtures


@tagged("post_install", "-at_install", "marketing_ad_preview")
class TestContactCenterAdPreviewBridge(SavepointCase):
    # Reuse the event fixture without inheriting its unrelated test methods.
    _source_touchpoint = (
        bridge_fixtures.TestMarketingContactCenterAttributionBridge._source_touchpoint
    )

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Ad preview bridge inbox",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "preview-" + str(uuid.uuid4()),
                "ad_preview_enrichment_enabled": True,
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Preview bridge transport",
                "account_id": cls.account.id,
                "adapter_key": "test.marketing.bridge",
                "external_ref": "preview-" + str(uuid.uuid4()),
                "provider_schema_version": "fixture-v1",
                "state": "connected",
            }
        )

    def setUp(self):
        super().setUp()
        if "marketing.center.meta.ad.preview.service" not in self.env:
            self.skipTest("Optional Meta integration is not installed")
        service = self.env["marketing.center.meta.ad.preview.service"]
        patcher = patch.object(
            type(service),
            "_fetch_ad_preview",
            return_value={"title": "Public ad title"},
        )
        self.fetch = patcher.start()
        self.addCleanup(patcher.stop)

    def _catalog_source(self, account_id):
        return self.env["marketing.center.source"].create(
            {
                "name": "Preview catalog " + account_id,
                "service": "meta.ads",
                "company_id": self.env.company.id,
                "external_account_ref": "act_" + account_id,
                "external_account_id": account_id,
                "currency_id": self.env.company.currency_id.id,
                "timezone": "UTC",
                "state": "active",
            }
        )

    def _entity(self, source, entity_type="ad", segment="ads"):
        result = self.env["marketing.center.catalog.service"]._upsert_entity(
            self.env.company,
            source,
            ExternalEntityDTO(
                entity_type=entity_type,
                external_ref=source.external_account_ref + "/" + segment + "/30",
                external_id="30",
                name="Internal ad label",
                observed_at=datetime.datetime(2026, 9, 12, 12),
            ),
        )
        return self.env["marketing.center.external.entity"].browse(result.entity_id)

    def _touchpoint(self, namespace="meta.ad_id", value="30"):
        source = self._source_touchpoint()
        self.env["contact.center.attribution.identifier"].with_context(
            contact_center_attribution_token=CONTACT_CENTER_ATTRIBUTION_TOKEN,
            marketing_contact_center_skip_enqueue=True,
        ).create(
            {
                "touchpoint_id": source.id,
                "inbox_event_id": source.inbox_event_id.id,
                "namespace": namespace,
                "role": "asset",
                "value": value,
                "comparison_hash": hashlib.sha256(value.encode()).hexdigest(),
                "source_field": "fixture.ad_id",
                "source_provider": "test.marketing.bridge",
                "observed_at": source.captured_at,
            }
        )
        return source

    def _preview(self, source, sync=True):
        if sync:
            self.env["marketing.contact.center.attribution.service"]._sync_touchpoint(
                source
            )
        return self.env["contact.center.attribution.preview"].new(
            {"touchpoint_id": source.id}
        )

    def test_exact_bridge_and_unique_ad_catalog_resolution_enrich(self):
        entity = self._entity(self._catalog_source("123"))
        preview = self._preview(self._touchpoint())
        self.assertEqual(preview._marketing_preview_entity(), entity)
        self.assertEqual(
            preview._marketing_preview_values(), {"title": "Public ad title"}
        )
        self.fetch.assert_called_once_with(entity)

    def test_unlinked_touchpoint_does_not_borrow_another_touchpoint_ad(self):
        self._entity(self._catalog_source("123"))
        self._preview(self._touchpoint())
        unrelated = self._preview(self._touchpoint(), sync=False)
        self.assertEqual(unrelated._marketing_preview_values(), {})
        self.fetch.assert_not_called()

    def test_ambiguous_ad_id_across_accounts_never_uses_arbitrary_reader(self):
        self._entity(self._catalog_source("123"))
        self._entity(self._catalog_source("456"))
        preview = self._preview(self._touchpoint())
        self.assertEqual(preview._marketing_preview_values(), {})
        self.fetch.assert_not_called()

    def test_meta_source_id_resolved_to_campaign_is_not_treated_as_ad(self):
        self._entity(self._catalog_source("123"), "campaign", "campaigns")
        preview = self._preview(self._touchpoint("meta.source_id"))
        self.assertEqual(preview._marketing_preview_values(), {})
        self.fetch.assert_not_called()

    def test_disabled_inbox_flag_stops_before_marketing_fetch(self):
        self._entity(self._catalog_source("123"))
        preview = self._preview(self._touchpoint())
        self.account.write({"ad_preview_enrichment_enabled": False})
        self.assertEqual(preview._marketing_preview_values(), {})
        self.fetch.assert_not_called()

    def test_stale_source_revision_does_not_use_older_link(self):
        self._entity(self._catalog_source("123"))
        source = self._touchpoint()
        preview = self._preview(source)
        source.with_context(
            contact_center_attribution_token=CONTACT_CENTER_ATTRIBUTION_TOKEN
        ).write({"utm_campaign": "new-revision"})
        self.assertEqual(preview._marketing_preview_values(), {})
        self.fetch.assert_not_called()

    def test_disabled_during_fetch_discards_enrichment(self):
        self._entity(self._catalog_source("123"))
        preview = self._preview(self._touchpoint())

        def revoke(entity):
            self.account.write({"ad_preview_enrichment_enabled": False})
            return {"title": "Must not be stored"}

        self.fetch.side_effect = revoke
        self.assertEqual(preview._marketing_preview_values(), {})

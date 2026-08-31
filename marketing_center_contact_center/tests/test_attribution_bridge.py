import uuid

from odoo.exceptions import AccessError
from odoo.tests.common import SavepointCase

from odoo.addons.contact_center_base.services.adapter import (
    ProviderAdapter,
    adapter_registry,
)
from odoo.addons.contact_center_base.services.dto import AdapterResult, EventDTO
from odoo.addons.queue_job.tests.common import trap_jobs


@adapter_registry.register("test.marketing.bridge")
class MarketingBridgeTestAdapter(ProviderAdapter):
    display_name = "Marketing Bridge Test"

    def authenticate_webhook(self, connection, headers, body):
        return True

    def normalize_event(self, connection, envelope):
        return EventDTO.from_dict(envelope)

    def execute_command(self, connection, command):
        return AdapterResult.success(external_message_id="unused")

    def prepare_request_snapshot(self, connection, command):
        return {"provider": self.key, "method": "POST", "endpoint": "/unused"}

    def get_capabilities(self, connection):
        return {}

    def get_health(self, connection):
        return {"state": "connected"}


class TestMarketingContactCenterAttributionBridge(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.team = cls.env["contact.center.team"].create(
            {"name": "Marketing Bridge Team", "company_id": cls.env.company.id}
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Marketing Bridge Account",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "bridge-account-%s" % uuid.uuid4(),
                "default_team_id": cls.team.id,
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Marketing Bridge Provider",
                "account_id": cls.account.id,
                "adapter_key": "test.marketing.bridge",
                "external_ref": "bridge-connection-%s" % uuid.uuid4(),
                "provider_schema_version": "fixture-v1",
                "state": "connected",
            }
        )

    def _source_touchpoint(self, suffix=None):
        suffix = suffix or str(uuid.uuid4())
        event = EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": "fixture-v1",
                "event_id": "bridge-event-%s" % suffix,
                "event_type": "message.created",
                "occurred_at": "2026-08-29T15:00:00Z",
                "account_ref": self.account.external_ref,
                "connection_ref": self.connection.external_ref,
                "conversation_ref": "bridge-conversation-%s" % suffix,
                "platform": "whatsapp",
                "direction": "inbound",
                "is_from_me": False,
                "origin": "provider",
                "actor": {
                    "display_name": "Bridge Person",
                    "addresses": [
                        {
                            "namespace": "whatsapp.pn",
                            "value": "5511999999999@s.whatsapp.net",
                            "value_normalized": "5511999999999@s.whatsapp.net",
                            "role": "primary",
                            "confidence": "protocol",
                            "resolution_scope": "company",
                        }
                    ],
                },
                "conversation": {
                    "conversation_type": "direct",
                    "addresses": [
                        {
                            "namespace": "whatsapp.pn",
                            "value": "5511999999999@s.whatsapp.net",
                            "value_normalized": "5511999999999@s.whatsapp.net",
                            "role": "primary",
                            "confidence": "protocol",
                            "resolution_scope": "company",
                        }
                    ],
                },
                "message": {
                    "external_message_id": "bridge-message-%s" % suffix,
                    "content_type": "text",
                    "text": "Bridge fixture",
                },
                "attribution": [
                    {
                        "touchpoint_type": "paid_ad_click",
                        "evidence_level": "provider_asserted",
                        "network": "meta",
                        "source_platform": "facebook",
                        "source_url": "https://example.test/landing?fbclid=secret",
                        "external_identifiers": [
                            {
                                "namespace": "meta.ctwa_clid",
                                "role": "click",
                                "value": "secret-click-id",
                                "source_field": "fixture.ctwa_clid",
                            }
                        ],
                        "utm": {"source": "facebook", "campaign": "solar"},
                    }
                ],
            }
        )
        inbox = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": "bridge:%s" % suffix,
                    "provider_schema_version": "fixture-v1",
                    "raw_envelope_json": event.to_dict(),
                }
            )
        )
        return self.env["contact.center.attribution.touchpoint"]._capture_event(
            self.connection, event, inbox
        )

    def test_bridge_extension_enqueues_safe_job_and_sync_is_idempotent(self):
        with trap_jobs() as trap:
            source = self._source_touchpoint()
            trap.assert_enqueued_job(
                source._job_sync_marketing_touchpoint,
                properties={
                    "identity_key": "marketing_contact_center:touchpoint:%s"
                    % source.public_ref
                },
            )
        source._job_sync_marketing_touchpoint()
        source._job_sync_marketing_touchpoint()
        links = (
            self.env["marketing.attribution.contact.center.link"]
            .sudo()
            .search([("source_touchpoint_id", "=", source.id)])
        )
        self.assertEqual(len(links), 1)
        target = links.marketing_touchpoint_id
        self.assertEqual(target.channel, "whatsapp")
        self.assertEqual(target.platform, "facebook")
        self.assertEqual(target.network, "meta")
        self.assertEqual(target.landing_url, "https://example.test/landing")
        self.assertEqual(target.identifier_ids.masked_value, "se...k-id")
        self.assertNotIn("secret-click-id", str(target.extensions_json))

    def test_direct_link_mutation_is_blocked(self):
        source = self._source_touchpoint()
        source._job_sync_marketing_touchpoint()
        link = (
            self.env["marketing.attribution.contact.center.link"]
            .sudo()
            .search([("source_touchpoint_id", "=", source.id)], limit=1)
        )
        with self.assertRaises(AccessError):
            link.write({"mapping_version": 2})
        with self.assertRaises(AccessError):
            link.unlink()

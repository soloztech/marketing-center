import uuid
from types import SimpleNamespace
from unittest.mock import patch

from psycopg2 import OperationalError, errors as pg_errors

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.contact_center_base.services.adapter import (
    ProviderAdapter,
    adapter_registry,
)
from odoo.addons.contact_center_base.services.dto import AdapterResult, EventDTO
from odoo.addons.contact_center_base.services.tokens import (
    CONTACT_CENTER_ATTRIBUTION_TOKEN,
)
from odoo.addons.marketing_center_base.services.dto import AttributionDTOValidationError
from odoo.addons.queue_job.exception import FailedJobError, RetryableJobError
from odoo.addons.queue_job.job import Job
from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.mapper import ContactCenterAttributionMapper
from ..services.tokens import MARKETING_CONTACT_CENTER_LINK_WRITE_TOKEN


def _synthetic_unique_violation(constraint_name):
    class SyntheticUniqueViolation(pg_errors.UniqueViolation):
        @property
        def diag(self):
            return SimpleNamespace(constraint_name=constraint_name)

    return SyntheticUniqueViolation("synthetic concurrent attribution revision")


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
                "access_team_ids": [(6, 0, cls.team.ids)],
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

    def _observed_touchpoint(self, stable_event_id, person_address, suffix):
        event = EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": "fixture-v1",
                "event_id": stable_event_id,
                "event_type": "attribution.observed",
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
                            "value": person_address,
                            "value_normalized": person_address,
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
                            "value": person_address,
                            "value_normalized": person_address,
                            "role": "primary",
                            "confidence": "protocol",
                            "resolution_scope": "company",
                        }
                    ],
                },
                "attribution": [
                    {
                        "touchpoint_type": "paid_ad_click",
                        "evidence_level": "provider_asserted",
                        "network": "meta",
                        "source_platform": "facebook",
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
                    "inbox_dedupe_key": "bridge-observed:%s" % suffix,
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
                    "identity_key": source._marketing_attribution_identity_key()
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

    def test_attribution_wakeups_are_transaction_scoped(self):
        source = self._source_touchpoint()
        first = source._marketing_attribution_identity_key()
        second = source._marketing_attribution_identity_key()
        backfill = source._marketing_attribution_identity_key("backfill")
        self.assertEqual(first, second)
        self.assertIn(":tx:", first)
        self.assertNotEqual(first, backfill)
        self.assertTrue(backfill.endswith(":v2:backfill"))
        with self.assertRaises(ValidationError):
            source._marketing_attribution_identity_key("invalid")

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

    def test_same_provider_event_in_distinct_conversations_never_collapses(self):
        stable_event_id = "bridge-shared-event-%s" % uuid.uuid4()
        first = self._observed_touchpoint(
            stable_event_id,
            "5511999999901@s.whatsapp.net",
            "first-%s" % uuid.uuid4(),
        )
        second = self._observed_touchpoint(
            stable_event_id,
            "5511999999902@s.whatsapp.net",
            "second-%s" % uuid.uuid4(),
        )
        self.assertNotEqual(first.canonical_key, second.canonical_key)
        self.assertEqual(first.source_external_key, second.source_external_key)

        first._job_sync_marketing_touchpoint()
        second._job_sync_marketing_touchpoint()
        links = (
            self.env["marketing.attribution.contact.center.link"]
            .sudo()
            .search([("source_touchpoint_id", "in", (first.id, second.id))])
        )
        self.assertEqual(len(links), 2)
        self.assertEqual(len(links.mapped("marketing_touchpoint_id")), 2)
        self.assertEqual(set(links.mapped("mapping_version")), {2})

    def test_contact_center_schema_upgrade_fails_closed_until_mapper_is_updated(self):
        source = self._source_touchpoint()
        with patch(
            "odoo.addons.marketing_center_contact_center.services.mapper."
            "ATTRIBUTION_SCHEMA_VERSION",
            2,
        ), self.assertRaisesRegex(
            AttributionDTOValidationError,
            "supports Contact Center AttributionDTO v1; received v2",
        ):
            ContactCenterAttributionMapper.to_dto(source)

    def test_link_model_rejects_a_non_current_mapper_version(self):
        source = self._source_touchpoint()
        source._job_sync_marketing_touchpoint()
        current = (
            self.env["marketing.attribution.contact.center.link"]
            .sudo()
            .search([("source_touchpoint_id", "=", source.id)], limit=1)
        )
        with self.assertRaisesRegex(ValidationError, "current bridge mapper"):
            (
                self.env["marketing.attribution.contact.center.link"]
                .with_context(
                    marketing_contact_center_link_write_token=(
                        MARKETING_CONTACT_CENTER_LINK_WRITE_TOKEN
                    )
                )
                .create(
                    {
                        "company_id": source.company_id.id,
                        "source_touchpoint_id": source.id,
                        "marketing_touchpoint_id": current.marketing_touchpoint_id.id,
                        "source_public_ref": source.public_ref,
                        "source_content_hash": current.source_content_hash,
                        "mapping_version": 1,
                    }
                )
            )

    def test_contact_center_enrichment_projects_one_effective_revision_chain(self):
        source = self._source_touchpoint()
        source._job_sync_marketing_touchpoint()
        first_link = (
            self.env["marketing.attribution.contact.center.link"]
            .sudo()
            .search([("source_touchpoint_id", "=", source.id)], limit=1)
        )
        source.with_context(
            contact_center_attribution_token=CONTACT_CENTER_ATTRIBUTION_TOKEN,
            marketing_contact_center_skip_enqueue=True,
        ).write({"enrichment_state": "enriched"})

        source._job_sync_marketing_touchpoint()

        links = (
            self.env["marketing.attribution.contact.center.link"]
            .sudo()
            .search([("source_touchpoint_id", "=", source.id)])
        )
        revisions = links.mapped("marketing_touchpoint_id").sorted("revision_sequence")
        self.assertEqual(len(links), 2)
        self.assertEqual(revisions.mapped("revision_sequence"), [1, 2])
        self.assertEqual(len(set(revisions.mapped("canonical_key"))), 1)
        self.assertEqual(
            revisions.mapped("revision_kind"), ["observation", "enrichment"]
        )
        effective = self.env["marketing.attribution.effective.touchpoint"].search(
            [
                ("company_id", "=", source.company_id.id),
                (
                    "canonical_key",
                    "=",
                    first_link.marketing_touchpoint_id.canonical_key,
                ),
            ]
        )
        self.assertEqual(len(effective), 1)
        self.assertEqual(effective.id, revisions[-1].id)
        self.assertEqual(effective.effective_disposition, "enriched")

    def test_optional_bridge_validation_never_aborts_contact_center_write(self):
        source = self._source_touchpoint()
        with patch.object(
            ContactCenterAttributionMapper,
            "to_dto",
            side_effect=AttributionDTOValidationError("future schema"),
        ):
            with trap_jobs() as trap:
                source.with_context(
                    contact_center_attribution_token=CONTACT_CENTER_ATTRIBUTION_TOKEN
                ).write({"utm_campaign": "schema-isolated"})
                trap.assert_enqueued_job(
                    source._job_sync_marketing_touchpoint,
                    properties={
                        "identity_key": source._marketing_attribution_identity_key()
                    },
                )
            self.assertEqual(source.utm_campaign, "schema-isolated")
            with self.assertRaisesRegex(
                AttributionDTOValidationError,
                "future schema",
            ):
                source._job_sync_marketing_touchpoint()

    def test_attribution_backfill_fans_out_one_isolated_job_per_source(self):
        first = self._source_touchpoint()
        second = self._source_touchpoint()
        service = self.env["marketing.contact.center.attribution.service"]
        with self.assertRaises(AccessError):
            service._enqueue_backfill(company=object())
        with self.assertRaises(AccessError):
            service._enqueue_backfill(company=self.env["res.company"])

        with trap_jobs() as trap:
            result = service._enqueue_backfill(
                company=self.env.company,
                after_id=first.id - 1,
                limit=2,
            )
            trap.assert_jobs_count(2)
            for source in (first, second):
                trap.assert_enqueued_job(
                    source._job_sync_marketing_touchpoint,
                    properties={
                        "identity_key": source._marketing_attribution_identity_key(
                            "backfill"
                        ),
                        "priority": 55,
                    },
                )

        self.assertEqual(result["enqueued"], 2)
        self.assertEqual(result["last_id"], second.id)

    def test_only_bridge_revision_unique_races_are_retryable(self):
        source = self._source_touchpoint()
        service_class = type(self.env["marketing.contact.center.attribution.service"])
        for constraint_name in (
            "marketing_attribution_touchpoint_canonical_revision_unique",
            "marketing_attr_cc_link_source_revision_unique",
            "marketing_attr_cc_link_target_mapping_uniq",
        ):
            known_race = _synthetic_unique_violation(constraint_name)
            with patch.object(
                service_class,
                "_sync_touchpoint",
                side_effect=known_race,
            ), self.assertRaises(RetryableJobError):
                source._job_sync_marketing_touchpoint()

    def test_transient_database_retries_exhaust_and_unknown_errors_fail(self):
        source = self._source_touchpoint()
        service_class = type(self.env["marketing.contact.center.attribution.service"])
        job = Job(source._job_sync_marketing_touchpoint, max_retries=8)
        with patch.object(
            service_class,
            "_sync_touchpoint",
            side_effect=pg_errors.SerializationFailure(),
        ):
            for attempt in range(1, 8):
                with self.assertRaises(RetryableJobError):
                    job.perform()
                self.assertEqual(job.retry, attempt)
            with self.assertRaises(FailedJobError):
                job.perform()
            self.assertEqual(job.retry, 8)
        with patch.object(
            service_class,
            "_sync_touchpoint",
            side_effect=OperationalError("unclassified"),
        ):
            with self.assertRaises(OperationalError):
                source._job_sync_marketing_touchpoint()

        unrelated = _synthetic_unique_violation(
            "marketing_attribution_touchpoint_public_ref_unique"
        )
        with patch.object(
            service_class,
            "_sync_touchpoint",
            side_effect=unrelated,
        ), self.assertRaises(pg_errors.UniqueViolation):
            source._job_sync_marketing_touchpoint()

    def test_unmapped_contact_center_write_does_not_enqueue(self):
        source = self._source_touchpoint()
        with trap_jobs() as trap:
            source.with_context(
                contact_center_attribution_token=CONTACT_CENTER_ATTRIBUTION_TOKEN
            ).write({"provider_extensions_json": {"unmapped": "evidence"}})
            trap.assert_jobs_count(0)

    def test_identifier_creation_enqueues_live_priority_sync(self):
        source = self._source_touchpoint()
        with trap_jobs() as trap:
            identifier = (
                self.env["contact.center.attribution.identifier"]
                .with_context(
                    contact_center_attribution_token=CONTACT_CENTER_ATTRIBUTION_TOKEN
                )
                .create(
                    {
                        "touchpoint_id": source.id,
                        "inbox_event_id": source.inbox_event_id.id,
                        "namespace": "test.bridge.extra",
                        "role": "attribution",
                        "value": "extra-identifier",
                        "comparison_hash": "b" * 64,
                        "source_field": "fixture.extra_identifier",
                        "source_provider": "test.marketing.bridge",
                        "observed_at": source.captured_at,
                    }
                )
            )
            trap.assert_enqueued_job(
                source._job_sync_marketing_touchpoint,
                properties={
                    "identity_key": source._marketing_attribution_identity_key(),
                    "priority": 40,
                },
            )
        self.assertEqual(identifier.touchpoint_id, source)

    def test_initial_contact_center_conflict_never_becomes_effective(self):
        source = self._source_touchpoint()
        source.with_context(
            contact_center_attribution_token=CONTACT_CENTER_ATTRIBUTION_TOKEN,
            marketing_contact_center_skip_enqueue=True,
        ).write({"conflict_state": "conflict"})

        source._job_sync_marketing_touchpoint()

        link = (
            self.env["marketing.attribution.contact.center.link"]
            .sudo()
            .search([("source_touchpoint_id", "=", source.id)], limit=1)
        )
        target = link.marketing_touchpoint_id
        self.assertEqual(target.revision_kind, "conflict")
        self.assertEqual(target.evidence_ids.disposition, "conflict")
        effective = self.env["marketing.attribution.effective.touchpoint"].search(
            [
                ("company_id", "=", source.company_id.id),
                ("canonical_key", "=", target.canonical_key),
            ]
        )
        self.assertFalse(effective)

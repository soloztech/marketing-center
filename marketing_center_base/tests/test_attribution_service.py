import datetime

from odoo.tests.common import SavepointCase

from ..services.dto import MarketingIdentifierDTO, MarketingTouchpointDTO, sha256_text


class TestMarketingAttributionService(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.service = cls.env["marketing.attribution.service"]

    def _dto(self, **overrides):
        values = {
            "source_system": "contact_center",
            "source_scope_ref": "provider-connection-public-ref",
            "source_occurrence_ref": "message:provider-message-id",
            "source_evidence_ref": "contact-center-touchpoint-public-ref",
            "occurred_at": datetime.datetime(2026, 8, 29, 13, 0),
            "platform": "whatsapp",
            "channel": "whatsapp",
            "network": "meta",
            "touchpoint_type": "paid_ad_signal",
            "evidence_level": "provider_asserted",
            "utm": {"source": "facebook", "campaign": "solar"},
            "identifiers": (
                MarketingIdentifierDTO(
                    namespace="meta.ctwa_clid",
                    role="click",
                    comparison_hash=sha256_text("ctwa-1"),
                    masked_value="ctw...a-1",
                ),
            ),
        }
        values.update(overrides)
        return MarketingTouchpointDTO(**values)

    def test_ingest_and_exact_replay_are_idempotent(self):
        first = self.service._ingest_touchpoint(self.company, self._dto())
        replay = self.service._ingest_touchpoint(
            self.company,
            self._dto(observed_at=datetime.datetime(2026, 8, 30, 8, 0)),
        )
        self.assertEqual(first.disposition, "accepted")
        self.assertEqual(replay.disposition, "duplicate")
        self.assertEqual(first.touchpoint_id, replay.touchpoint_id)
        touchpoint = self.env["marketing.attribution.touchpoint"].browse(
            first.touchpoint_id
        )
        self.assertEqual(touchpoint.revision_sequence, 1)
        self.assertEqual(len(touchpoint.identifier_ids), 1)
        self.assertEqual(len(touchpoint.evidence_ids), 1)

    def test_changed_content_creates_conflict_revision_without_overwrite(self):
        first = self.service._ingest_touchpoint(self.company, self._dto())
        conflict = self.service._ingest_touchpoint(
            self.company, self._dto(utm={"source": "facebook", "campaign": "wind"})
        )
        self.assertEqual(conflict.disposition, "conflict")
        self.assertNotEqual(first.touchpoint_id, conflict.touchpoint_id)
        original = self.env["marketing.attribution.touchpoint"].browse(
            first.touchpoint_id
        )
        revision = self.env["marketing.attribution.touchpoint"].browse(
            conflict.touchpoint_id
        )
        self.assertEqual(original.utm_campaign, "solar")
        self.assertEqual(revision.utm_campaign, "wind")
        self.assertEqual(revision.revision_sequence, 2)
        self.assertEqual(revision.evidence_ids.disposition, "conflict")
        self.assertEqual(revision.evidence_ids.related_touchpoint_id, original)

    def test_new_evidence_or_mapper_version_preserves_lineage_without_duplication(self):
        first = self.service._ingest_touchpoint(self.company, self._dto())
        second = self.service._ingest_touchpoint(
            self.company,
            self._dto(
                source_evidence_ref="contact-center-touchpoint-second-observation"
            ),
        )
        mapped_again = self.service._ingest_touchpoint(
            self.company,
            self._dto(
                source_evidence_ref="contact-center-touchpoint-second-observation",
                mapping_version=2,
            ),
        )
        self.assertEqual(first.touchpoint_id, second.touchpoint_id)
        self.assertEqual(first.touchpoint_id, mapped_again.touchpoint_id)
        touchpoint = self.env["marketing.attribution.touchpoint"].browse(
            first.touchpoint_id
        )
        self.assertEqual(len(touchpoint.evidence_ids), 3)
        self.assertEqual(set(touchpoint.evidence_ids.mapped("mapping_version")), {1, 2})

    def test_company_is_outside_portable_canonical_key(self):
        other_company = self.env["res.company"].create({"name": "Other Company"})
        first = self.service._ingest_touchpoint(self.company, self._dto())
        other_service = self.service.with_context(
            allowed_company_ids=[self.company.id, other_company.id]
        )
        second = other_service._ingest_touchpoint(other_company, self._dto())
        self.assertEqual(first.canonical_key, second.canonical_key)
        self.assertNotEqual(first.touchpoint_id, second.touchpoint_id)

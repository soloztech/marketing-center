import datetime
import uuid

from odoo.exceptions import AccessError
from odoo.tests.common import SavepointCase

from ..services.dto import MarketingTouchpointDTO


class TestMarketingAttributionEffectiveTouchpoint(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service = cls.env["marketing.attribution.service"]
        cls.occurrence_ref = "effective:%s" % uuid.uuid4()

    def _dto(self, **overrides):
        values = {
            "source_system": "test.effective",
            "source_scope_ref": "effective-suite",
            "source_occurrence_ref": self.occurrence_ref,
            "source_evidence_ref": "evidence:%s" % uuid.uuid4(),
            "occurred_at": datetime.datetime(2026, 9, 1, 12, 0),
            "platform": "web",
            "channel": "website",
            "touchpoint_type": "entry_point",
            "evidence_level": "first_party",
            "utm": {"source": "google", "campaign": "accepted"},
        }
        values.update(overrides)
        return MarketingTouchpointDTO(**values)

    def _effective(self, canonical_key):
        return self.env["marketing.attribution.effective.touchpoint"].search(
            [
                ("company_id", "=", self.env.company.id),
                ("canonical_key", "=", canonical_key),
            ]
        )

    def test_latest_accepted_revision_wins_and_conflict_is_not_effective(self):
        accepted = self.service._ingest_touchpoint(self.env.company, self._dto())
        conflict = self.service._ingest_touchpoint(
            self.env.company,
            self._dto(utm={"source": "google", "campaign": "conflict"}),
        )

        effective = self._effective(accepted.canonical_key)
        self.assertEqual(len(effective), 1)
        self.assertEqual(effective.id, accepted.touchpoint_id)
        self.assertNotEqual(effective.id, conflict.touchpoint_id)
        self.assertEqual(effective.effective_disposition, "accepted")
        self.assertEqual(effective.utm_campaign, "accepted")

    def test_initial_conflict_is_evidence_but_never_effective(self):
        conflict = self.service._ingest_touchpoint(
            self.env.company,
            self._dto(revision_kind="conflict"),
        )

        self.assertEqual(conflict.disposition, "conflict")
        self.assertFalse(self._effective(conflict.canonical_key))
        touchpoint = self.env["marketing.attribution.touchpoint"].browse(
            conflict.touchpoint_id
        )
        self.assertEqual(touchpoint.revision_sequence, 1)
        self.assertEqual(touchpoint.evidence_ids.disposition, "conflict")

    def test_enrichment_and_correction_replace_the_current_projection(self):
        accepted = self.service._ingest_touchpoint(self.env.company, self._dto())
        enriched = self.service._ingest_touchpoint(
            self.env.company,
            self._dto(
                revision_kind="enrichment",
                asset_refs={"meta.ad_id": "123"},
            ),
        )
        effective = self._effective(accepted.canonical_key)
        self.assertEqual(effective.id, enriched.touchpoint_id)
        self.assertEqual(effective.effective_disposition, "enriched")

        corrected = self.service._ingest_touchpoint(
            self.env.company,
            self._dto(
                revision_kind="correction",
                utm={"source": "google", "campaign": "corrected"},
            ),
        )
        effective = self._effective(accepted.canonical_key)
        self.assertEqual(effective.id, corrected.touchpoint_id)
        self.assertEqual(effective.effective_disposition, "revised")
        self.assertEqual(effective.revision_sequence, 3)

    def test_projection_is_one_row_per_company_and_canonical_key(self):
        accepted = self.service._ingest_touchpoint(self.env.company, self._dto())
        self.service._ingest_touchpoint(
            self.env.company,
            self._dto(
                revision_kind="enrichment",
                utm={"source": "google", "campaign": "enriched"},
            ),
        )
        other_company = self.env["res.company"].create({"name": "Effective Other"})
        other_result = self.service.with_context(
            allowed_company_ids=[self.env.company.id, other_company.id]
        )._ingest_touchpoint(other_company, self._dto())

        rows = (
            self.env["marketing.attribution.effective.touchpoint"]
            .with_context(allowed_company_ids=[self.env.company.id, other_company.id])
            .search([("canonical_key", "=", accepted.canonical_key)])
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            set(rows.mapped("company_id")), {self.env.company, other_company}
        )
        self.assertIn(other_result.touchpoint_id, rows.ids)

    def test_projection_is_read_only_and_does_not_widen_raw_acl(self):
        result = self.service._ingest_touchpoint(self.env.company, self._dto())
        effective = self._effective(result.canonical_key)
        viewer_group = self.env.ref(
            "marketing_center_base.group_marketing_center_viewer"
        )
        viewer = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Effective touchpoint viewer",
                    "login": "effective-viewer-%s" % uuid.uuid4(),
                    "email": "effective-viewer@example.invalid",
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [(6, 0, viewer_group.ids)],
                }
            )
        )
        with self.assertRaises(AccessError):
            effective.with_user(viewer).read(["public_ref"])
        with self.assertRaises(AccessError):
            effective.sudo().write({"utm_campaign": "forbidden"})
        with self.assertRaises(AccessError):
            self.env["marketing.attribution.effective.touchpoint"].sudo().create(
                {"company_id": self.env.company.id}
            )

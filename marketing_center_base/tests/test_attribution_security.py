import datetime

from odoo.exceptions import AccessError
from odoo.tests.common import SavepointCase

from ..services.dto import MarketingIdentifierDTO, MarketingTouchpointDTO, sha256_text


class TestMarketingAttributionSecurity(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.dto = MarketingTouchpointDTO(
            source_system="website",
            source_scope_ref="www.example.com",
            source_occurrence_ref="session:event-1",
            occurred_at=datetime.datetime(2026, 8, 29, 14, 0),
            platform="web",
            channel="website",
            touchpoint_type="entry_point",
            evidence_level="first_party",
            identifiers=(
                MarketingIdentifierDTO(
                    namespace="google.gclid",
                    role="click",
                    comparison_hash=sha256_text("gclid-value"),
                ),
            ),
        )
        result = cls.env["marketing.attribution.service"]._ingest_touchpoint(
            cls.env.company, cls.dto
        )
        cls.touchpoint = cls.env["marketing.attribution.touchpoint"].browse(
            result.touchpoint_id
        )
        cls.identifier = cls.touchpoint.identifier_ids
        cls.viewer = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Marketing Viewer Test",
                    "login": "marketing-viewer-test",
                    "email": "marketing-viewer@example.test",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, [cls.env.company.id])],
                    "groups_id": [
                        (
                            6,
                            0,
                            [
                                cls.env.ref(
                                    "marketing_center_base.group_marketing_center_viewer"
                                ).id
                            ],
                        )
                    ],
                }
            )
        )

    def test_direct_mutation_is_blocked_even_with_sudo(self):
        with self.assertRaises(AccessError):
            self.env["marketing.attribution.touchpoint"].sudo().create(
                {"company_id": self.env.company.id}
            )
        with self.assertRaises(AccessError):
            self.touchpoint.sudo().write({"utm_campaign": "changed"})
        with self.assertRaises(AccessError):
            self.touchpoint.sudo().unlink()

    def test_unresolved_touchpoint_is_admin_only(self):
        with self.assertRaises(AccessError):
            self.touchpoint.with_user(self.viewer).read(["public_ref", "utm_campaign"])
        with self.assertRaises(AccessError):
            self.identifier.with_user(self.viewer).read(["comparison_hash"])
        self.touchpoint.read(["public_ref", "utm_campaign"])

    def test_viewer_cannot_read_another_company_touchpoint(self):
        other_company = self.env["res.company"].create({"name": "Marketing Other"})
        service = self.env["marketing.attribution.service"].with_context(
            allowed_company_ids=[self.env.company.id, other_company.id]
        )
        result = service._ingest_touchpoint(other_company, self.dto)
        other_touchpoint = self.env["marketing.attribution.touchpoint"].browse(
            result.touchpoint_id
        )
        with self.assertRaises(AccessError):
            other_touchpoint.with_user(self.viewer).with_context(
                allowed_company_ids=[self.env.company.id]
            ).read(["public_ref"])

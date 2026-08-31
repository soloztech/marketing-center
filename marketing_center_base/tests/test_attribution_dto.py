import datetime

from odoo.tests.common import TransactionCase

from ..services.dto import (
    AttributionDTOValidationError,
    MarketingIdentifierDTO,
    MarketingTouchpointDTO,
    sha256_text,
)


class TestMarketingAttributionDTO(TransactionCase):
    def _dto(self, **overrides):
        values = {
            "source_system": "contact_center",
            "source_scope_ref": "connection:abc",
            "source_occurrence_ref": "message:123",
            "occurred_at": datetime.datetime(2026, 8, 29, 12, 30),
            "platform": "whatsapp",
            "channel": "whatsapp",
            "network": "meta",
            "touchpoint_type": "paid_ad_signal",
            "evidence_level": "provider_asserted",
            "landing_url": "https://Example.com/path?gclid=secret&utm_source=meta#fragment",
            "identifiers": (
                MarketingIdentifierDTO(
                    namespace="meta.ctwa_clid",
                    role="click",
                    comparison_hash=sha256_text("click-123"),
                    masked_value="clic...123",
                ),
            ),
        }
        values.update(overrides)
        return MarketingTouchpointDTO(**values)

    def test_roundtrip_and_url_sanitization(self):
        dto = self._dto()
        self.assertEqual(dto.landing_url, "https://example.com/path")
        self.assertEqual(
            MarketingTouchpointDTO.from_dict(dto.to_dict()).to_dict(), dto.to_dict()
        )

    def test_accepts_utc_z_timestamp_on_the_wire(self):
        dto = MarketingTouchpointDTO.from_dict(
            {
                **self._dto().to_dict(),
                "occurred_at": "2026-08-29T12:30:00Z",
                "observed_at": "2026-08-29T12:31:00Z",
                "privacy": {
                    "consent_state": "unknown",
                    "decided_at": "2026-08-29T12:29:00Z",
                },
            }
        )
        self.assertEqual(dto.occurred_at, datetime.datetime(2026, 8, 29, 12, 30))
        self.assertEqual(dto.observed_at, datetime.datetime(2026, 8, 29, 12, 31))
        self.assertEqual(dto.privacy.decided_at, datetime.datetime(2026, 8, 29, 12, 29))

    def test_key_is_stable_and_content_detects_change(self):
        first = self._dto()
        replay = self._dto(observed_at=datetime.datetime(2026, 8, 30, 10, 0))
        changed = self._dto(utm={"campaign": "new-campaign"})
        self.assertEqual(first.canonical_key, replay.canonical_key)
        self.assertEqual(first.content_hash, replay.content_hash)
        self.assertEqual(first.canonical_key, changed.canonical_key)
        self.assertNotEqual(first.content_hash, changed.content_hash)

    def test_rejects_invalid_schema_and_identifier(self):
        with self.assertRaises(AttributionDTOValidationError):
            self._dto(schema_version=2)
        with self.assertRaises(AttributionDTOValidationError):
            MarketingIdentifierDTO(
                namespace="meta.ctwa_clid", role="click", comparison_hash="raw"
            )

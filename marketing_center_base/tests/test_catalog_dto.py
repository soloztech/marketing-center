import datetime

from odoo.tests.common import TransactionCase

from ..services.catalog_dto import (
    CatalogDTOValidationError,
    ExternalEntityDTO,
    SyncPageDTO,
)
from ..services.dto import sha256_text


class TestMarketingCatalogDTO(TransactionCase):
    def _entity(self, **overrides):
        values = {
            "entity_type": "campaign",
            "external_ref": "customers/AbC/campaigns/Camp-42",
            "external_id": "Camp-42",
            "name": "Campaign A",
            "remote_status": "ACTIVE",
            "observed_at": datetime.datetime(2026, 8, 30, 9, 30),
            "provider_updated_at": datetime.datetime(2026, 8, 29, 9, 30),
            "attributes": {"budget": {"amount_micros": 12500000}},
            "source_schema_version": "v21.0",
        }
        values.update(overrides)
        return ExternalEntityDTO(**values)

    def test_roundtrip_iso_z_and_case_sensitive_identifiers(self):
        entity = ExternalEntityDTO.from_dict(
            {
                **self._entity().to_dict(),
                "observed_at": "2026-08-30T09:30:00Z",
                "provider_updated_at": "2026-08-01T10:00:00Z",
            }
        )
        self.assertEqual(entity.external_id, "Camp-42")
        self.assertEqual(entity.remote_status, "active")
        self.assertEqual(entity.observed_at, datetime.datetime(2026, 8, 30, 9, 30))
        self.assertEqual(entity.to_dict()["observed_at"], "2026-08-30T09:30:00Z")
        self.assertEqual(
            ExternalEntityDTO.from_dict(entity.to_dict()).to_dict(),
            entity.to_dict(),
        )
        other_case = self._entity(external_ref="customers/abc/campaigns/Camp-42")
        self.assertNotEqual(entity.canonical_key, other_case.canonical_key)

    def test_hash_ignores_observation_time_and_preserves_a_b_a(self):
        first_a = self._entity()
        replay_a = self._entity(
            observed_at=datetime.datetime(2026, 8, 30, 11, 45),
            provider_updated_at=datetime.datetime(2026, 8, 30, 11, 30),
        )
        state_b = self._entity(name="Campaign B")
        tombstone = self._entity(
            remote_missing_at=datetime.datetime(2026, 8, 31, 11, 30)
        )
        second_a = self._entity()
        self.assertEqual(first_a.canonical_key, replay_a.canonical_key)
        self.assertEqual(first_a.content_hash, replay_a.content_hash)
        self.assertNotEqual(first_a.content_hash, state_b.content_hash)
        self.assertNotEqual(first_a.content_hash, tombstone.content_hash)
        self.assertEqual(first_a.content_hash, second_a.content_hash)

    def test_rejects_schema_unknown_fields_and_secret_bearing_json(self):
        with self.assertRaises(CatalogDTOValidationError):
            self._entity(schema_version=2)
        with self.assertRaises(CatalogDTOValidationError):
            ExternalEntityDTO.from_dict(
                {**self._entity().to_dict(), "provider_payload": "unsupported"}
            )
        with self.assertRaises(CatalogDTOValidationError):
            self._entity(attributes={"nested": {"accessToken": "do-not-store"}})
        with self.assertRaises(CatalogDTOValidationError):
            self._entity(attributes={"safe": "x" * (32 * 1024)})
        with self.assertRaises(CatalogDTOValidationError):
            self._entity(parent_external_ref="Account-AbC")

    def test_sync_page_roundtrip_and_async_contract(self):
        page = SyncPageDTO.from_dict(
            {
                "items": [self._entity().to_dict()],
                "next_cursor": "Cursor-AbC",
                "has_more": True,
                "provider_request_id": "Request-XyZ",
                "provider_job_ref": "Job-AbC",
                "provider_job_state": "RUNNING",
                "watermark": "Watermark-AbC",
                "reporting_context_hash": sha256_text("account-daily-utc"),
                "retry_after": 30,
                "errors": [{"external_ref": "Campaign-Missing", "code": "not_found"}],
            }
        )
        self.assertEqual(page.next_cursor, "Cursor-AbC")
        self.assertEqual(page.provider_job_ref, "Job-AbC")
        self.assertEqual(page.provider_job_state, "running")
        self.assertEqual(page.watermark, "Watermark-AbC")
        self.assertEqual(
            SyncPageDTO.from_dict(page.to_dict()).to_dict(), page.to_dict()
        )

    def test_sync_page_rejects_ambiguous_pagination_and_duplicates(self):
        with self.assertRaises(CatalogDTOValidationError):
            SyncPageDTO(has_more=True)
        with self.assertRaises(CatalogDTOValidationError):
            SyncPageDTO(provider_job_ref="Job-1")
        with self.assertRaises(CatalogDTOValidationError):
            SyncPageDTO(items=(self._entity(), self._entity()))
        with self.assertRaises(CatalogDTOValidationError):
            SyncPageDTO(retry_after=True)

import datetime

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from ..services.catalog_dto import ExternalEntityDTO
from ..services.tokens import MARKETING_CATALOG_WRITE_TOKEN


class TestMarketingCatalogService(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = cls.env["marketing.center.source"].create(
            {
                "name": "Catalog source",
                "company_id": cls.env.company.id,
                "service": "google.ads",
                "external_account_ref": "customers/Case-123",
                "currency_id": cls.env.company.currency_id.id,
                "timezone": "UTC",
                "state": "active",
            }
        )
        cls.service = cls.env["marketing.center.catalog.service"]

    def _dto(self, **overrides):
        values = {
            "entity_type": "campaign",
            "external_ref": "customers/Case-123/campaigns/Camp-A",
            "external_id": "Camp-A",
            "name": "Campaign A",
            "remote_status": "active",
            "observed_at": datetime.datetime(2026, 8, 30, 10, 0),
            "attributes": {"google.bidding_strategy": "maximize_conversions"},
        }
        values.update(overrides)
        return ExternalEntityDTO(**values)

    def test_exact_replay_and_a_b_a_revision_history(self):
        first = self.service._upsert_entity(self.env.company, self.source, self._dto())
        replay = self.service._upsert_entity(
            self.env.company,
            self.source,
            self._dto(observed_at=datetime.datetime(2026, 8, 30, 11, 0)),
        )
        state_b = self.service._upsert_entity(
            self.env.company, self.source, self._dto(name="Campaign B")
        )
        second_a = self.service._upsert_entity(
            self.env.company, self.source, self._dto()
        )
        self.assertEqual(first.entity_id, replay.entity_id)
        self.assertEqual(replay.disposition, "duplicate")
        self.assertEqual(state_b.revision_sequence, 2)
        self.assertEqual(second_a.revision_sequence, 3)
        entity = self.env["marketing.center.external.entity"].browse(first.entity_id)
        self.assertEqual(
            entity.revision_ids.sorted("revision_sequence").mapped("revision_sequence"),
            [1, 2, 3],
        )
        self.assertEqual(entity.current_revision_sequence, 3)
        self.assertEqual(entity.name, "Campaign A")

    def test_tombstone_is_revision_and_ledgers_are_protected(self):
        first = self.service._upsert_entity(
            self.env.company,
            self.source,
            self._dto(external_ref="customers/Case-123/campaigns/Tombstone"),
        )
        tombstone = self.service._upsert_entity(
            self.env.company,
            self.source,
            self._dto(
                external_ref="customers/Case-123/campaigns/Tombstone",
                remote_missing_at=datetime.datetime(2026, 8, 31, 10, 0),
            ),
        )
        self.assertEqual(tombstone.disposition, "tombstone")
        entity = self.env["marketing.center.external.entity"].browse(first.entity_id)
        self.assertEqual(len(entity.revision_ids), 2)
        self.assertTrue(entity.current_revision_id.is_tombstone)
        with self.assertRaises(AccessError):
            entity.sudo().write({"name": "Direct mutation"})
        with self.assertRaises(AccessError):
            entity.current_revision_id.sudo().write({"name": "Direct mutation"})
        with self.assertRaises(AccessError):
            entity.unlink()

    def test_parent_cannot_cross_source_or_cycle(self):
        other_source = self.env["marketing.center.source"].create(
            {
                "name": "Other catalog source",
                "company_id": self.env.company.id,
                "service": "google.ads",
                "external_account_ref": "customers/Other",
                "currency_id": self.env.company.currency_id.id,
                "timezone": "UTC",
                "state": "active",
            }
        )
        parent_result = self.service._upsert_entity(
            self.env.company,
            other_source,
            ExternalEntityDTO(
                entity_type="account",
                external_ref="customers/Other",
                name="Other",
                observed_at=datetime.datetime(2026, 8, 30, 10, 0),
            ),
        )
        parent = self.env["marketing.center.external.entity"].browse(
            parent_result.entity_id
        )
        child_result = self.service._upsert_entity(
            self.env.company,
            self.source,
            self._dto(external_ref="customers/Case-123/campaigns/Child"),
        )
        child = self.env["marketing.center.external.entity"].browse(
            child_result.entity_id
        )
        with self.assertRaises(ValidationError):
            child.with_context(
                marketing_catalog_write_token=MARKETING_CATALOG_WRITE_TOKEN
            ).write({"parent_id": parent.id})
        with self.assertRaises(ValidationError):
            child.with_context(
                marketing_catalog_write_token=MARKETING_CATALOG_WRITE_TOKEN
            ).write({"parent_id": child.id})

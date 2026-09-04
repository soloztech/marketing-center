import uuid

from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase
from odoo.tools import mute_logger

from ..services.tokens import MARKETING_CONFIGURATION_RUNTIME_TOKEN


class TestMarketingSourceConnection(SavepointCase):
    def _source(self, **overrides):
        values = {
            "name": "Meta Ads Laboratory",
            "company_id": self.env.company.id,
            "service": "meta.ads",
            "external_account_ref": "act_Lab-AbC",
            "currency_id": self.env.company.currency_id.id,
            "timezone": "America/Sao_Paulo",
            "state": "active",
        }
        values.update(overrides)
        return self.env["marketing.center.source"].create(values)

    def _connection(self, source, **overrides):
        values = {
            "name": "Meta reader",
            "source_id": source.id,
            "adapter_key": "meta.graph",
            "purpose": "reader",
            "profile_public_ref": "profile-meta-lab-reader",
            "profile_revision": 3,
            "state": "ready",
        }
        values.update(overrides)
        return self.env["marketing.center.connection"].create(values)

    def test_source_identity_preserves_case_and_rejects_duplicates(self):
        source = self._source()
        self.assertEqual(source.external_account_ref, "act_Lab-AbC")
        other_case = self._source(name="Other case", external_account_ref="act_lab-abc")
        self.assertNotEqual(source, other_case)
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"):
            with self.env.cr.savepoint():
                self._source(name="Duplicate")
        with self.assertRaises(ValidationError):
            self._source(
                name="Invalid timezone",
                external_account_ref="other",
                timezone="Brazil/Not_A_Zone",
            )

    def test_scheduler_batch_rotates_past_the_lowest_source_ids(self):
        service = "fair.scheduler.%s" % uuid.uuid4().hex
        sources = self.env["marketing.center.source"]
        for position in range(3):
            sources |= self._source(
                name="Fair source %s" % position,
                service=service,
                external_account_ref="fair_%s_%s" % (position, uuid.uuid4().hex),
            )
        cursor_key = "test.fair.%s" % uuid.uuid4().hex
        domain = [
            ("active", "=", True),
            ("service", "=", service),
            ("state", "=", "active"),
        ]
        selected = []
        for _iteration in range(4):
            batch = self.env["marketing.center.source"]._fair_scheduler_batch(
                domain,
                cursor_key=cursor_key,
                limit=1,
            )
            selected.append(batch.id)

        ordered = sources.sorted("id").ids
        self.assertEqual(selected, ordered + ordered[:1])

    def test_scheduler_batch_rejects_unbounded_or_unsafe_arguments(self):
        source_model = self.env["marketing.center.source"]
        with self.assertRaises(ValidationError):
            source_model._fair_scheduler_batch([], cursor_key="../unsafe", limit=1)
        with self.assertRaises(ValidationError):
            source_model._fair_scheduler_batch([], cursor_key="safe", limit=0)

    def test_scheduler_wraps_companies_and_rolls_back_cursor(self):
        other_company = self.env["res.company"].create(
            {"name": "Fair scheduler company %s" % uuid.uuid4().hex}
        )
        service = "fair.multi.%s" % uuid.uuid4().hex
        sources = self.env["marketing.center.source"]
        for position, company in enumerate(
            (self.env.company, other_company, self.env.company)
        ):
            sources |= self._source(
                name="Multi-company fair source %s" % position,
                company_id=company.id,
                service=service,
                external_account_ref="multi_%s_%s" % (position, uuid.uuid4().hex),
            )
        cursor_key = "test.multi.%s" % uuid.uuid4().hex
        domain = [("service", "=", service)]
        first = self.env["marketing.center.source"]._fair_scheduler_batch(
            domain,
            cursor_key=cursor_key,
            limit=2,
        )
        rolled_back_ids = []
        with self.assertRaises(RuntimeError):
            with self.env.cr.savepoint():
                rolled_back_ids.extend(
                    self.env["marketing.center.source"]
                    ._fair_scheduler_batch(
                        domain,
                        cursor_key=cursor_key,
                        limit=2,
                    )
                    .ids
                )
                raise RuntimeError("rollback scheduler cursor")
        repeated = self.env["marketing.center.source"]._fair_scheduler_batch(
            domain,
            cursor_key=cursor_key,
            limit=2,
        )

        ordered = sources.sorted("id").ids
        self.assertEqual(first.ids, ordered[:2])
        self.assertEqual(rolled_back_ids, ordered[2:] + ordered[:1])
        self.assertEqual(repeated.ids, rolled_back_ids)

    def test_connection_rotation_is_fenced_and_runtime_is_protected(self):
        source = self._source()
        connection = self._connection(source)
        source_revision = source.configuration_revision
        source.write({"state": "paused"})
        self.assertEqual(source.configuration_revision, source_revision + 1)
        source.write({"state": "active"})
        self.assertEqual(source.configuration_revision, source_revision + 2)
        self.assertEqual(connection.binding_revision, 1)
        connection.write(
            {"profile_public_ref": "profile-meta-lab-reader-v2", "profile_revision": 4}
        )
        self.assertEqual(connection.binding_revision, 2)
        connection.write({"state": "paused"})
        self.assertEqual(connection.binding_revision, 3)
        connection.write({"state": "ready"})
        self.assertEqual(connection.binding_revision, 4)
        with self.assertRaises(AccessError):
            connection.write({"health_state": "healthy"})
        connection.with_context(
            marketing_configuration_runtime_token=MARKETING_CONFIGURATION_RUNTIME_TOKEN
        ).write({"health_state": "healthy"})
        self.assertEqual(connection.health_state, "healthy")

    def test_first_release_blocks_write_capability_and_deletion(self):
        source = self._source(external_account_ref="act_%s" % uuid.uuid4())
        connection = self._connection(source)
        with self.assertRaises(AccessError):
            source.write({"write_enabled": True})
        with self.assertRaises(AccessError):
            source.unlink()
        with self.assertRaises(AccessError):
            connection.unlink()

    def test_revision_increment_uses_locked_database_value(self):
        source = self._source(external_account_ref="act_%s" % uuid.uuid4())
        connection = self._connection(source)
        self.assertEqual(source.configuration_revision, 1)
        self.assertEqual(connection.binding_revision, 1)
        self.env.cr.execute(
            "UPDATE marketing_center_source SET configuration_revision = 5 "
            "WHERE id = %s",
            [source.id],
        )
        self.env.cr.execute(
            "UPDATE marketing_center_connection SET binding_revision = 7 "
            "WHERE id = %s",
            [connection.id],
        )
        source.write({"state": "paused"})
        connection.write({"state": "paused"})
        self.assertEqual(source.configuration_revision, 6)
        self.assertEqual(connection.binding_revision, 8)

    def test_connection_source_identity_is_immutable(self):
        source = self._source(external_account_ref="act_%s" % uuid.uuid4())
        other = self._source(external_account_ref="act_%s" % uuid.uuid4())
        connection = self._connection(source)
        with self.assertRaises(AccessError):
            connection.write({"source_id": other.id})

    def test_source_external_identity_freezes_after_first_binding(self):
        source = self._source(external_account_ref="draft_%s" % uuid.uuid4())
        revision = source.configuration_revision
        source.write(
            {
                "service": "google.ads",
                "external_account_ref": "customers/%s" % uuid.uuid4().int,
                "external_account_id": "1234567890",
            }
        )
        self.assertEqual(source.service, "google.ads")
        self.assertEqual(source.configuration_revision, revision + 1)
        self._connection(source, adapter_key="google.ads", profile_revision=0)
        with self.assertRaises(AccessError):
            source.write({"external_account_ref": "customers/reassigned"})
        with self.assertRaises(AccessError):
            source.write({"external_account_id": "9999999999"})
        with self.assertRaises(AccessError):
            source.write({"service": "meta.ads"})

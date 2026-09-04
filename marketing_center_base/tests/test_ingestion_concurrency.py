import datetime
import uuid

from psycopg2 import errorcodes

from odoo import SUPERUSER_ID, api
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from ..services.business_event_dto import MarketingBusinessEventDTO
from ..services.catalog_dto import ExternalEntityDTO
from ..services.dto import MarketingTouchpointDTO
from ..services.performance_dto import MarketingPerformanceDTO
from ..services.serialization import (
    MarketingSerializationFailure,
    acquire_advisory_xact_lock,
)


@tagged("-at_install", "post_install")
class TestMarketingCoreIngestionConcurrency(TransactionCase):
    """Exercise contention with independent PostgreSQL transactions."""

    def _environment(self, cr, company_id):
        return api.Environment(
            cr,
            SUPERUSER_ID,
            {"allowed_company_ids": [company_id]},
        )

    def _assert_serialization(self, callable_):
        with self.assertRaises(MarketingSerializationFailure) as caught:
            callable_()
        self.assertEqual(caught.exception.pgcode, errorcodes.SERIALIZATION_FAILURE)

    def _touchpoint_dto(self, suffix):
        return MarketingTouchpointDTO(
            source_system="test.concurrent.attribution",
            source_scope_ref="concurrency-suite",
            source_occurrence_ref="touchpoint:%s" % suffix,
            source_evidence_ref="touchpoint-evidence:%s" % suffix,
            occurred_at=datetime.datetime(2026, 9, 3, 12, 0),
            platform="web",
            channel="website",
            touchpoint_type="entry_point",
            evidence_level="first_party",
        )

    def _business_event_dto(self, suffix, **overrides):
        values = {
            "event_class": "lifecycle",
            "event_type": "lead_created",
            "source_system": "test.concurrent.business.%s" % suffix,
            "source_model": "test.record",
            "source_res_id": 1,
            "source_occurrence_ref": "business-occurrence:%s" % suffix,
            "source_evidence_ref": "business-evidence:%s" % suffix,
            "business_event_key": "business-event:%s" % suffix,
            "occurred_at": datetime.datetime(2026, 9, 3, 12, 0),
            "evidence_level": "first_party",
        }
        values.update(overrides)
        return MarketingBusinessEventDTO.from_dict(values)

    def _create_source_fixture(self):
        suffix = uuid.uuid4().hex
        company_id = self.env.company.id
        with self.registry.cursor() as cr:
            env = self._environment(cr, company_id)
            company = env["res.company"].browse(company_id)
            source = env["marketing.center.source"].create(
                {
                    "name": "Concurrent ingestion %s" % suffix,
                    "company_id": company_id,
                    "service": "test.concurrent",
                    "external_account_ref": "test-account-%s" % suffix,
                    "currency_id": company.currency_id.id,
                    "timezone": "UTC",
                    "state": "active",
                }
            )
            result = {
                "company_id": company_id,
                "currency": company.currency_id.name,
                "source_id": source.id,
                "suffix": suffix,
            }
            cr.commit()  # pylint: disable=invalid-commit
            return result

    def _cleanup_source_fixture(self, fixture):
        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT id FROM marketing_center_external_entity "
                "WHERE source_id = %s",
                [fixture["source_id"]],
            )
            entity_ids = [row[0] for row in cr.fetchall()]
            if entity_ids:
                cr.execute(
                    "DELETE FROM marketing_attribution_asset_resolution "
                    "WHERE entity_id = ANY(%s)",
                    [entity_ids],
                )
                cr.execute(
                    "UPDATE marketing_center_external_entity "
                    "SET current_revision_id = NULL WHERE id = ANY(%s)",
                    [entity_ids],
                )
                cr.execute(
                    "DELETE FROM marketing_center_external_entity_revision "
                    "WHERE entity_id = ANY(%s)",
                    [entity_ids],
                )
                cr.execute(
                    "DELETE FROM marketing_center_external_entity WHERE id = ANY(%s)",
                    [entity_ids],
                )
            cr.execute(
                "SELECT id FROM marketing_center_metric_daily WHERE source_id = %s",
                [fixture["source_id"]],
            )
            metric_ids = [row[0] for row in cr.fetchall()]
            if metric_ids:
                cr.execute(
                    "UPDATE marketing_center_metric_daily "
                    "SET current_revision_id = NULL WHERE id = ANY(%s)",
                    [metric_ids],
                )
                cr.execute(
                    "DELETE FROM marketing_center_metric_revision "
                    "WHERE metric_id = ANY(%s)",
                    [metric_ids],
                )
                cr.execute(
                    "DELETE FROM marketing_center_metric_daily WHERE id = ANY(%s)",
                    [metric_ids],
                )
            cr.execute(
                "DELETE FROM marketing_center_source_access_user_rel "
                "WHERE source_id = %s",
                [fixture["source_id"]],
            )
            cr.execute(
                "DELETE FROM marketing_center_source WHERE id = %s",
                [fixture["source_id"]],
            )
            cr.commit()  # pylint: disable=invalid-commit

    def _cleanup_touchpoint(self, canonical_key):
        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT id FROM marketing_attribution_touchpoint "
                "WHERE canonical_key = %s",
                [canonical_key],
            )
            touchpoint_ids = [row[0] for row in cr.fetchall()]
            if touchpoint_ids:
                cr.execute(
                    "DELETE FROM marketing_attribution_asset_resolution "
                    "WHERE touchpoint_id = ANY(%s)",
                    [touchpoint_ids],
                )
                cr.execute(
                    "DELETE FROM marketing_attribution_identifier "
                    "WHERE touchpoint_id = ANY(%s)",
                    [touchpoint_ids],
                )
                cr.execute(
                    "DELETE FROM marketing_attribution_evidence "
                    "WHERE touchpoint_id = ANY(%s)",
                    [touchpoint_ids],
                )
                cr.execute(
                    "DELETE FROM marketing_attribution_touchpoint "
                    "WHERE id = ANY(%s)",
                    [touchpoint_ids],
                )
            cr.commit()  # pylint: disable=invalid-commit

    def _cleanup_business_events(self, source_system):
        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT id FROM marketing_business_event WHERE source_system = %s",
                [source_system],
            )
            event_ids = [row[0] for row in cr.fetchall()]
            if event_ids:
                cr.execute(
                    "DELETE FROM marketing_business_event_observation "
                    "WHERE event_id = ANY(%s)",
                    [event_ids],
                )
                cr.execute(
                    "UPDATE marketing_business_event "
                    "SET reverses_event_id = NULL, root_event_id = NULL "
                    "WHERE id = ANY(%s)",
                    [event_ids],
                )
                cr.execute(
                    "DELETE FROM marketing_business_event WHERE id = ANY(%s)",
                    [event_ids],
                )
            cr.commit()  # pylint: disable=invalid-commit

    def test_touchpoint_contention_retries_and_converges(self):
        suffix = uuid.uuid4().hex
        company_id = self.env.company.id
        dto = self._touchpoint_dto(suffix)
        lock_key = "marketing_attribution:%s:%s" % (company_id, dto.canonical_key)
        try:
            with self.registry.cursor() as owner_cr:
                owner_env = self._environment(owner_cr, company_id)
                acquire_advisory_xact_lock(owner_cr, lock_key)
                with self.registry.cursor() as contender_cr:
                    contender_env = self._environment(contender_cr, company_id)
                    self._assert_serialization(
                        lambda: contender_env[
                            "marketing.attribution.service"
                        ]._ingest_touchpoint(
                            contender_env["res.company"].browse(company_id), dto
                        )
                    )
                    contender_cr.rollback()
                accepted = owner_env[
                    "marketing.attribution.service"
                ]._ingest_touchpoint(owner_env["res.company"].browse(company_id), dto)
                owner_cr.commit()  # pylint: disable=invalid-commit

            with self.registry.cursor() as retry_cr:
                retry_env = self._environment(retry_cr, company_id)
                duplicate = retry_env[
                    "marketing.attribution.service"
                ]._ingest_touchpoint(retry_env["res.company"].browse(company_id), dto)
                self.assertEqual(accepted.touchpoint_id, duplicate.touchpoint_id)
                self.assertEqual(duplicate.disposition, "duplicate")
                retry_cr.rollback()
        finally:
            self._cleanup_touchpoint(dto.canonical_key)

    def test_business_event_contention_retries_and_converges(self):
        suffix = uuid.uuid4().hex
        company_id = self.env.company.id
        dto = self._business_event_dto(suffix)
        lock_key = "marketing_business_event:%s:%s" % (
            company_id,
            dto.canonical_key,
        )
        try:
            with self.registry.cursor() as owner_cr:
                owner_env = self._environment(owner_cr, company_id)
                acquire_advisory_xact_lock(owner_cr, lock_key)
                with self.registry.cursor() as contender_cr:
                    contender_env = self._environment(contender_cr, company_id)
                    self._assert_serialization(
                        lambda: contender_env[
                            "marketing.business.event.service"
                        ]._ingest_event(
                            contender_env["res.company"].browse(company_id), dto
                        )
                    )
                    contender_cr.rollback()
                accepted = owner_env["marketing.business.event.service"]._ingest_event(
                    owner_env["res.company"].browse(company_id), dto
                )
                owner_cr.commit()  # pylint: disable=invalid-commit

            with self.registry.cursor() as retry_cr:
                retry_env = self._environment(retry_cr, company_id)
                duplicate = retry_env["marketing.business.event.service"]._ingest_event(
                    retry_env["res.company"].browse(company_id), dto
                )
                self.assertEqual(accepted.event_id, duplicate.event_id)
                self.assertEqual(duplicate.disposition, "duplicate")
                retry_cr.rollback()
        finally:
            self._cleanup_business_events(dto.source_system)

    def test_catalog_contention_retries_and_converges(self):
        fixture = self._create_source_fixture()
        dto = ExternalEntityDTO(
            entity_type="campaign",
            external_ref="test-account-%s/campaigns/1" % fixture["suffix"],
            external_id="1",
            name="Concurrent campaign",
            observed_at=datetime.datetime(2026, 9, 3, 12, 0),
        )
        lock_key = "marketing_catalog:%s:%s:%s" % (
            fixture["source_id"],
            dto.entity_type,
            dto.external_ref,
        )
        try:
            with self.registry.cursor() as owner_cr:
                owner_env = self._environment(owner_cr, fixture["company_id"])
                acquire_advisory_xact_lock(owner_cr, lock_key)
                with self.registry.cursor() as contender_cr:
                    contender_env = self._environment(
                        contender_cr, fixture["company_id"]
                    )
                    self._assert_serialization(
                        lambda: contender_env[
                            "marketing.center.catalog.service"
                        ]._upsert_entity(
                            contender_env["res.company"].browse(fixture["company_id"]),
                            contender_env["marketing.center.source"].browse(
                                fixture["source_id"]
                            ),
                            dto,
                        )
                    )
                    contender_cr.rollback()
                accepted = owner_env["marketing.center.catalog.service"]._upsert_entity(
                    owner_env["res.company"].browse(fixture["company_id"]),
                    owner_env["marketing.center.source"].browse(fixture["source_id"]),
                    dto,
                )
                owner_cr.commit()  # pylint: disable=invalid-commit

            with self.registry.cursor() as retry_cr:
                retry_env = self._environment(retry_cr, fixture["company_id"])
                duplicate = retry_env[
                    "marketing.center.catalog.service"
                ]._upsert_entity(
                    retry_env["res.company"].browse(fixture["company_id"]),
                    retry_env["marketing.center.source"].browse(fixture["source_id"]),
                    dto,
                )
                self.assertEqual(accepted.entity_id, duplicate.entity_id)
                self.assertEqual(duplicate.disposition, "duplicate")
                retry_cr.rollback()
        finally:
            self._cleanup_source_fixture(fixture)

    def test_performance_contention_retries_and_converges(self):
        fixture = self._create_source_fixture()
        dto = MarketingPerformanceDTO(
            grain="campaign",
            entity_external_ref="test-account-%s/campaigns/1" % fixture["suffix"],
            report_date=datetime.date(2026, 9, 3),
            period_start_utc=datetime.datetime(2026, 9, 3, 0, 0),
            period_end_utc=datetime.datetime(2026, 9, 4, 0, 0),
            report_timezone="UTC",
            currency=fixture["currency"],
            observed_at=datetime.datetime(2026, 9, 4, 12, 0),
            impressions=100,
            clicks=10,
            cost_micros=1_000_000,
        )
        lock_key = "marketing_performance:%s:%s" % (
            fixture["source_id"],
            dto.canonical_key,
        )
        try:
            with self.registry.cursor() as owner_cr:
                owner_env = self._environment(owner_cr, fixture["company_id"])
                acquire_advisory_xact_lock(owner_cr, lock_key)
                with self.registry.cursor() as contender_cr:
                    contender_env = self._environment(
                        contender_cr, fixture["company_id"]
                    )
                    self._assert_serialization(
                        lambda: contender_env[
                            "marketing.center.performance.service"
                        ]._upsert_metric(
                            contender_env["res.company"].browse(fixture["company_id"]),
                            contender_env["marketing.center.source"].browse(
                                fixture["source_id"]
                            ),
                            dto,
                        )
                    )
                    contender_cr.rollback()
                accepted = owner_env[
                    "marketing.center.performance.service"
                ]._upsert_metric(
                    owner_env["res.company"].browse(fixture["company_id"]),
                    owner_env["marketing.center.source"].browse(fixture["source_id"]),
                    dto,
                )
                owner_cr.commit()  # pylint: disable=invalid-commit

            with self.registry.cursor() as retry_cr:
                retry_env = self._environment(retry_cr, fixture["company_id"])
                duplicate = retry_env[
                    "marketing.center.performance.service"
                ]._upsert_metric(
                    retry_env["res.company"].browse(fixture["company_id"]),
                    retry_env["marketing.center.source"].browse(fixture["source_id"]),
                    dto,
                )
                self.assertEqual(accepted.metric_id, duplicate.metric_id)
                self.assertEqual(duplicate.disposition, "duplicate")
                retry_cr.rollback()
        finally:
            self._cleanup_source_fixture(fixture)

    def test_concurrent_credit_rechecks_cumulative_ceiling_after_retry(self):
        suffix = uuid.uuid4().hex
        company_id = self.env.company.id
        invoice = self._business_event_dto(
            suffix,
            event_class="revenue",
            event_type="invoice_posted",
            source_res_id=10,
            amount_signed="100.00",
            currency="BRL",
        )
        with self.registry.cursor() as setup_cr:
            setup_env = self._environment(setup_cr, company_id)
            setup_env["marketing.business.event.service"]._ingest_event(
                setup_env["res.company"].browse(company_id), invoice
            )
            setup_cr.commit()  # pylint: disable=invalid-commit

        first_credit = self._business_event_dto(
            "%s:first-credit" % suffix,
            event_class="revenue",
            event_type="credit_note_posted",
            source_system=invoice.source_system,
            source_res_id=11,
            amount_signed="-70.00",
            currency="BRL",
            reverses_business_event_key=invoice.business_event_key,
        )
        second_credit = self._business_event_dto(
            "%s:second-credit" % suffix,
            event_class="revenue",
            event_type="credit_note_posted",
            source_system=invoice.source_system,
            source_res_id=12,
            amount_signed="-40.00",
            currency="BRL",
            reverses_business_event_key=invoice.business_event_key,
        )
        lock_key = "marketing_business_event_reversal:%s:%s:%s" % (
            company_id,
            invoice.source_system,
            invoice.business_event_key,
        )
        try:
            with self.registry.cursor() as owner_cr:
                owner_env = self._environment(owner_cr, company_id)
                acquire_advisory_xact_lock(owner_cr, lock_key)
                with self.registry.cursor() as contender_cr:
                    contender_env = self._environment(contender_cr, company_id)
                    self._assert_serialization(
                        lambda: contender_env[
                            "marketing.business.event.service"
                        ]._ingest_event(
                            contender_env["res.company"].browse(company_id),
                            second_credit,
                        )
                    )
                    contender_cr.rollback()
                owner_env["marketing.business.event.service"]._ingest_event(
                    owner_env["res.company"].browse(company_id), first_credit
                )
                owner_cr.commit()  # pylint: disable=invalid-commit

            with self.registry.cursor() as retry_cr:
                retry_env = self._environment(retry_cr, company_id)
                with self.assertRaises(ValidationError):
                    retry_env["marketing.business.event.service"]._ingest_event(
                        retry_env["res.company"].browse(company_id),
                        second_credit,
                    )
                retry_cr.rollback()

            with self.registry.cursor() as verify_cr:
                verify_cr.execute(
                    "SELECT COUNT(*), COALESCE(SUM(amount_signed_micros), 0) "
                    "FROM marketing_business_event "
                    "WHERE source_system = %s AND event_type = 'credit_note_posted'",
                    [invoice.source_system],
                )
                self.assertEqual(verify_cr.fetchone(), (1, -70_000_000))
                verify_cr.rollback()
        finally:
            self._cleanup_business_events(invoice.source_system)

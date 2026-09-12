import datetime
import uuid

from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import SavepointCase, TransactionCase

from ..services.business_event_dto import MarketingBusinessEventDTO


class TestMarketingBusinessEventService(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service = cls.env["marketing.business.event.service"]

    def _dto(self, suffix=None, **overrides):
        suffix = suffix or str(uuid.uuid4())
        values = {
            "event_class": "lifecycle",
            "event_type": "lead_created",
            "source_system": "odoo.crm",
            "source_model": "crm.lead",
            "source_res_id": 42,
            "source_occurrence_ref": "crm.lead:42:create:%s" % suffix,
            "source_evidence_ref": "crm.lead:42:%s" % suffix,
            "business_event_key": "crm.lead:42:created:%s" % suffix,
            "occurred_at": datetime.datetime(2026, 9, 1, 12, 0),
            "evidence_level": "first_party",
            "extensions": {"crm.lead_type": "lead"},
        }
        values.update(overrides)
        return MarketingBusinessEventDTO.from_dict(values)

    def test_accept_duplicate_and_conflict_are_explicit(self):
        dto = self._dto()
        accepted = self.service._ingest_event(self.env.company, dto)
        duplicate = self.service._ingest_event(self.env.company, dto)
        conflict = self.service._ingest_event(
            self.env.company,
            self._dto(
                business_event_key=dto.business_event_key,
                source_occurrence_ref=dto.source_occurrence_ref,
                source_evidence_ref="conflicting-observation",
                occurred_at=datetime.datetime(2026, 9, 1, 12, 1),
            ),
        )

        self.assertEqual(accepted.event_id, duplicate.event_id)
        self.assertEqual(accepted.event_id, conflict.event_id)
        self.assertEqual(accepted.disposition, "accepted")
        self.assertEqual(duplicate.disposition, "duplicate")
        self.assertEqual(conflict.disposition, "conflict")
        self.assertNotEqual(conflict.content_hash, accepted.content_hash)
        self.assertTrue(conflict.observation_id)
        event = self.env["marketing.business.event"].browse(accepted.event_id)
        self.assertEqual(len(event.observation_ids), 2)
        self.assertEqual(
            set(event.observation_ids.mapped("disposition")), {"accepted", "conflict"}
        )
        conflicting = event.observation_ids.filtered(
            lambda observation: observation.disposition == "conflict"
        )
        self.assertEqual(
            conflicting.snapshot_json["occurred_at"],
            "2026-09-01T12:01:00",
        )

    def test_new_evidence_reference_is_duplicate_not_content_conflict(self):
        dto = self._dto()
        first = self.service._ingest_event(self.env.company, dto)
        duplicate = self.service._ingest_event(
            self.env.company,
            self._dto(
                business_event_key=dto.business_event_key,
                source_occurrence_ref=dto.source_occurrence_ref,
                source_evidence_ref="second-observer",
            ),
        )
        self.assertEqual(first.event_id, duplicate.event_id)
        self.assertEqual(duplicate.disposition, "duplicate")
        event = self.env["marketing.business.event"].browse(first.event_id)
        self.assertEqual(len(event.observation_ids), 2)

    def test_reversal_is_immutable_and_points_to_root(self):
        original_dto = self._dto(
            event_class="revenue",
            event_type="order_confirmed",
            amount_signed="100.00",
            currency="BRL",
        )
        original_result = self.service._ingest_event(self.env.company, original_dto)
        reversal_dto = self._dto(
            event_class="revenue",
            event_type="order_cancelled",
            business_event_key="%s:cancelled" % original_dto.business_event_key,
            source_occurrence_ref="%s:cancelled" % original_dto.source_occurrence_ref,
            source_evidence_ref="cancelled:%s" % original_dto.source_evidence_ref,
            amount_signed="-100.00",
            currency="BRL",
            reverses_business_event_key=original_dto.business_event_key,
        )
        reversal_result = self.service._ingest_event(self.env.company, reversal_dto)
        original = self.env["marketing.business.event"].browse(original_result.event_id)
        reversal = self.env["marketing.business.event"].browse(reversal_result.event_id)
        self.assertEqual(reversal.reverses_event_id, original)
        self.assertEqual(reversal.root_event_id, original)
        with self.assertRaises(AccessError):
            original.write({"event_type": "won"})
        with self.assertRaises(AccessError):
            original.unlink()

    def test_direct_creation_and_missing_reversal_are_rejected(self):
        with self.assertRaises(AccessError):
            self.env["marketing.business.event"].sudo().create({})
        with self.assertRaises(ValidationError):
            self.service._ingest_event(
                self.env.company,
                self._dto(
                    event_class="revenue",
                    event_type="order_cancelled",
                    amount_signed="-1",
                    currency="BRL",
                    reverses_business_event_key="missing-original",
                ),
            )

    def test_multiple_credit_notes_can_reference_one_invoice(self):
        invoice_dto = self._dto(
            event_class="revenue",
            event_type="invoice_posted",
            source_res_id=501,
            amount_signed="100.00",
            currency="BRL",
        )
        invoice = self.service._ingest_event(self.env.company, invoice_dto)
        credit_event_ids = []
        for source_res_id, amount in ((601, "-30.00"), (602, "-20.00")):
            credit_dto = self._dto(
                event_class="revenue",
                event_type="credit_note_posted",
                source_res_id=source_res_id,
                amount_signed=amount,
                currency="BRL",
                reverses_business_event_key=invoice_dto.business_event_key,
            )
            credit_event_ids.append(
                self.service._ingest_event(self.env.company, credit_dto).event_id
            )
        invoice_event = self.env["marketing.business.event"].browse(invoice.event_id)
        credit_events = self.env["marketing.business.event"].browse(credit_event_ids)
        self.assertEqual(len(credit_events), 2)
        self.assertEqual(credit_events.mapped("reverses_event_id"), invoice_event)

    def test_cumulative_credit_cannot_exceed_the_invoice(self):
        invoice_dto = self._dto(
            event_class="revenue",
            event_type="invoice_posted",
            source_res_id=701,
            amount_signed="100.00",
            currency="BRL",
        )
        self.service._ingest_event(self.env.company, invoice_dto)
        self.service._ingest_event(
            self.env.company,
            self._dto(
                event_class="revenue",
                event_type="credit_note_posted",
                source_res_id=702,
                amount_signed="-70.00",
                currency="BRL",
                reverses_business_event_key=invoice_dto.business_event_key,
            ),
        )

        with self.assertRaises(ValidationError):
            self.service._ingest_event(
                self.env.company,
                self._dto(
                    event_class="revenue",
                    event_type="credit_note_posted",
                    source_res_id=703,
                    amount_signed="-40.00",
                    currency="BRL",
                    reverses_business_event_key=invoice_dto.business_event_key,
                ),
            )

    def test_six_decimal_amount_matches_snapshot_and_numeric_column(self):
        dto = self._dto(
            event_class="revenue",
            event_type="invoice_posted",
            amount_signed="1.234567",
            currency="BRL",
        )
        result = self.service._ingest_event(self.env.company, dto)
        event = self.env["marketing.business.event"].browse(result.event_id)
        self.env.cr.execute(
            "SELECT amount_signed_micros::text "
            "FROM marketing_business_event WHERE id = %s",
            [event.id],
        )
        self.assertEqual(self.env.cr.fetchone()[0], "1234567")
        self.assertEqual(event.snapshot_json["amount_signed"], "1.234567")

    def test_large_six_decimal_amount_remains_exact(self):
        dto = self._dto(
            event_class="revenue",
            event_type="invoice_posted",
            amount_signed="123456789012345.123456",
            currency="BRL",
        )
        result = self.service._ingest_event(self.env.company, dto)
        self.env.cr.execute(
            "SELECT amount_signed_micros::text "
            "FROM marketing_business_event WHERE id = %s",
            [result.event_id],
        )
        self.assertEqual(self.env.cr.fetchone()[0], "123456789012345123456")

    def test_reversal_currency_and_amount_are_exact(self):
        original = self._dto(
            event_class="revenue",
            event_type="order_confirmed",
            amount_signed="100.123456",
            currency="BRL",
        )
        self.service._ingest_event(self.env.company, original)
        values = {
            "event_class": "revenue",
            "event_type": "order_cancelled",
            "business_event_key": "%s:cancel" % original.business_event_key,
            "source_occurrence_ref": "%s:cancel" % original.source_occurrence_ref,
            "source_evidence_ref": "%s:cancel" % original.source_evidence_ref,
            "reverses_business_event_key": original.business_event_key,
        }
        with self.assertRaises(ValidationError):
            self.service._ingest_event(
                self.env.company,
                self._dto(amount_signed="-100.123455", currency="BRL", **values),
            )
        with self.assertRaises(ValidationError):
            self.service._ingest_event(
                self.env.company,
                self._dto(amount_signed="-100.123456", currency="USD", **values),
            )

    def test_zero_value_order_can_be_cancelled_exactly(self):
        original = self._dto(
            event_class="revenue",
            event_type="order_confirmed",
            amount_signed="0",
            currency="BRL",
        )
        original_result = self.service._ingest_event(self.env.company, original)
        cancelled = self._dto(
            event_class="revenue",
            event_type="order_cancelled",
            business_event_key="%s:cancel" % original.business_event_key,
            source_occurrence_ref="%s:cancel" % original.source_occurrence_ref,
            source_evidence_ref="%s:cancel" % original.source_evidence_ref,
            reverses_business_event_key=original.business_event_key,
            amount_signed="0",
            currency="BRL",
        )
        cancelled_result = self.service._ingest_event(self.env.company, cancelled)

        event = self.env["marketing.business.event"].browse(cancelled_result.event_id)
        self.assertEqual(event.amount_signed_micros, 0)
        self.assertEqual(event.reverses_event_id.id, original_result.event_id)

    def test_credit_unposting_releases_budget_and_preserves_invoice_root(self):
        invoice_dto = self._dto(
            event_class="revenue",
            event_type="invoice_posted",
            source_res_id=801,
            amount_signed="100",
            currency="BRL",
        )
        invoice_result = self.service._ingest_event(self.env.company, invoice_dto)
        invoice = self.env["marketing.business.event"].browse(invoice_result.event_id)
        credit_dto = self._dto(
            event_class="revenue",
            event_type="credit_note_posted",
            source_res_id=802,
            amount_signed="-100",
            currency="BRL",
            reverses_business_event_key=invoice_dto.business_event_key,
        )
        credit_result = self.service._ingest_event(self.env.company, credit_dto)
        credit = self.env["marketing.business.event"].browse(credit_result.event_id)
        counter_dto = self._dto(
            event_class="revenue",
            event_type="credit_note_posting_reversed",
            source_res_id=802,
            amount_signed="100",
            currency="BRL",
            reverses_business_event_key=credit_dto.business_event_key,
        )
        counter_result = self.service._ingest_event(self.env.company, counter_dto)
        counter = self.env["marketing.business.event"].browse(counter_result.event_id)
        self.assertEqual(counter.reverses_event_id, credit)
        self.assertEqual(counter.root_event_id, invoice)
        self.assertEqual(self.service._active_credit_amount_micros(invoice), 0)
        duplicate = self.service._ingest_event(self.env.company, counter_dto)
        self.assertEqual(duplicate.event_id, counter.id)
        self.assertEqual(duplicate.disposition, "duplicate")
        with self.assertRaises(ValidationError):
            self.service._ingest_event(
                self.env.company,
                self._dto(
                    event_class="revenue",
                    event_type="credit_note_posting_reversed",
                    source_res_id=802,
                    amount_signed="100",
                    currency="BRL",
                    reverses_business_event_key=credit_dto.business_event_key,
                ),
            )
        repost = self.service._ingest_event(
            self.env.company,
            self._dto(
                event_class="revenue",
                event_type="credit_note_posted",
                source_res_id=802,
                amount_signed="-100",
                currency="BRL",
                reverses_business_event_key=invoice_dto.business_event_key,
            ),
        )
        self.assertNotEqual(repost.event_id, credit.id)
        self.assertEqual(
            self.service._active_credit_amount_micros(invoice), -100_000_000
        )
        # An invoice can be unposted despite having a surviving credit. Its
        # counterevent invalidates only that invoice posting, never the credit.
        self.service._ingest_event(
            self.env.company,
            self._dto(
                event_class="revenue",
                event_type="invoice_posting_reversed",
                source_res_id=801,
                amount_signed="-100",
                currency="BRL",
                reverses_business_event_key=invoice_dto.business_event_key,
            ),
        )
        self.assertFalse(self.service._active_posting_events(invoice))
        self.assertEqual(
            self.service._active_credit_amount_micros(invoice), -100_000_000
        )

    def test_posting_reversal_rejects_wrong_amount_type_and_source(self):
        original = self._dto(
            event_class="revenue",
            event_type="invoice_posted",
            source_res_id=901,
            amount_signed="100",
            currency="BRL",
        )
        self.service._ingest_event(self.env.company, original)
        values = dict(
            event_class="revenue",
            event_type="invoice_posting_reversed",
            source_res_id=901,
            amount_signed="-100",
            currency="BRL",
            reverses_business_event_key=original.business_event_key,
        )
        for changes in (
            {"amount_signed": "-99"},
            {"currency": "USD"},
            {"source_res_id": 902},
            {"event_type": "credit_note_posting_reversed", "amount_signed": "100"},
        ):
            with self.subTest(changes=changes), self.assertRaises(
                ValidationError
            ), self.env.cr.savepoint():
                self.service._ingest_event(
                    self.env.company, self._dto(**{**values, **changes})
                )


@tagged("-at_install", "post_install")
class TestMarketingBusinessEventPostingConcurrency(TransactionCase):
    def test_credit_unposting_serializes_with_a_new_credit(self):
        source = "test.posting.%s" % uuid.uuid4().hex
        company_id = self.env.company.id

        def dto(event_type, key, source_res_id, amount, reverses=""):
            return MarketingBusinessEventDTO(
                event_class="revenue",
                event_type=event_type,
                source_system=source,
                source_model="account.move",
                source_res_id=source_res_id,
                source_occurrence_ref=key,
                business_event_key=key,
                occurred_at=datetime.datetime(2026, 9, 1),
                evidence_level="first_party",
                amount_signed=amount,
                currency="BRL",
                reverses_business_event_key=reverses,
            )

        try:
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                company = env["res.company"].browse(company_id)
                service = env["marketing.business.event.service"]
                service._ingest_event(
                    company, dto("invoice_posted", "invoice", 1, "100")
                )
                service._ingest_event(
                    company, dto("credit_note_posted", "credit", 2, "-100", "invoice")
                )
                cr.commit()  # pylint: disable=invalid-commit
            with self.registry.cursor() as owner_cr, self.registry.cursor() as waiter_cr:
                owner = api.Environment(owner_cr, SUPERUSER_ID, {})
                waiter = api.Environment(waiter_cr, SUPERUSER_ID, {})
                new_credit = dto(
                    "credit_note_posted", "new-credit", 3, "-100", "invoice"
                )
                owner["marketing.business.event.service"]._ingest_event(
                    owner["res.company"].browse(company_id),
                    dto(
                        "credit_note_posting_reversed",
                        "credit-void",
                        2,
                        "100",
                        "credit",
                    ),
                )
                with self.assertRaises(SerializationFailure):
                    waiter["marketing.business.event.service"]._ingest_event(
                        waiter["res.company"].browse(company_id),
                        new_credit,
                    )
                waiter_cr.rollback()
                owner_cr.commit()  # pylint: disable=invalid-commit
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                service = env["marketing.business.event.service"]
                result = service._ingest_event(
                    env["res.company"].browse(company_id), new_credit
                )
                self.assertEqual(result.disposition, "accepted")
                invoice = env["marketing.business.event"].search(
                    [
                        ("source_system", "=", source),
                        ("business_event_key", "=", "invoice"),
                    ]
                )
                self.assertEqual(
                    service._active_credit_amount_micros(invoice), -100_000_000
                )
                # No commit: only the initial fixture and owner's counterevent
                # need explicit cleanup below; the assertion transaction rolls back.
        finally:
            with self.registry.cursor() as cr:
                cr.execute(
                    "DELETE FROM marketing_business_event_observation "
                    "WHERE event_id IN (SELECT id FROM marketing_business_event "
                    "WHERE source_system = %s)",
                    [source],
                )
                cr.execute(
                    "DELETE FROM marketing_business_event WHERE source_system = %s",
                    [source],
                )
                cr.commit()  # pylint: disable=invalid-commit

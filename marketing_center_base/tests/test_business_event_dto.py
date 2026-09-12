import datetime
import decimal

from odoo.tests.common import TransactionCase

from ..services.business_event_dto import (
    BusinessEventDTOValidationError,
    MarketingBusinessEventDTO,
)


class TestMarketingBusinessEventDTO(TransactionCase):
    def _dto(self, **overrides):
        values = {
            "event_class": "lifecycle",
            "event_type": "lead_created",
            "source_system": "odoo.crm",
            "source_model": "crm.lead",
            "source_res_id": 42,
            "source_occurrence_ref": "crm.lead:42:create",
            "business_event_key": "crm.lead:42:created",
            "occurred_at": datetime.datetime(2026, 9, 1, 12, 0),
            "evidence_level": "first_party",
            "source_evidence_ref": "crm.lead:42",
            "extensions": {"crm.lead_type": "lead"},
        }
        values.update(overrides)
        return MarketingBusinessEventDTO.from_dict(values)

    def test_wire_roundtrip_and_identity_are_stable(self):
        dto = self._dto()
        replay = MarketingBusinessEventDTO.from_dict(dto.to_dict())
        self.assertEqual(dto.canonical_key, replay.canonical_key)
        self.assertEqual(dto.content_hash, replay.content_hash)
        self.assertNotEqual(
            dto.content_hash,
            self._dto(occurred_at=datetime.datetime(2026, 9, 1, 12, 1)).content_hash,
        )

    def test_interaction_started_is_a_provider_neutral_lifecycle_fact(self):
        dto = self._dto(event_type="interaction_started")
        self.assertEqual(dto.event_class, "lifecycle")
        self.assertEqual(dto.event_type, "interaction_started")

    def test_exact_amount_currency_and_reversal_contract(self):
        dto = self._dto(
            event_class="revenue",
            event_type="order_confirmed",
            amount_signed="1234.560000",
            currency="brl",
        )
        self.assertEqual(dto.amount_signed, decimal.Decimal("1234.560000"))
        self.assertEqual(dto.currency, "BRL")
        self.assertEqual(dto.to_dict()["amount_signed"], "1234.56")

        invalid = (
            {"amount_signed": "1.00"},
            {"currency": "BRL"},
            {"amount_signed": 1.1, "currency": "BRL"},
            {"amount_signed": "1.0000001", "currency": "BRL"},
            {"reverses_business_event_key": "crm.lead:42:created"},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(
                BusinessEventDTOValidationError
            ):
                self._dto(**values)

    def test_zero_value_order_and_exact_cancellation_are_valid(self):
        confirmed = self._dto(
            event_class="revenue",
            event_type="order_confirmed",
            amount_signed="0",
            currency="BRL",
        )
        cancelled = self._dto(
            event_class="revenue",
            event_type="order_cancelled",
            business_event_key="sale.order:42:cancelled",
            source_occurrence_ref="sale.order:42:cancelled",
            amount_signed="0",
            currency="BRL",
            reverses_business_event_key=confirmed.business_event_key,
        )

        self.assertEqual(confirmed.amount_signed, decimal.Decimal("0"))
        self.assertEqual(cancelled.amount_signed, decimal.Decimal("0"))

    def test_contract_rejects_unknown_semantics_and_unbounded_extensions(self):
        invalid = (
            {"event_class": "other"},
            {"event_type": "arbitrary"},
            {"source_res_id": 0},
            {"source_model": "crm lead"},
            {"extensions": {"not_namespaced": "x"}},
            {"extensions": {"crm.": "x"}},
            {"extensions": {"crm..field": "x"}},
            {
                "event_class": "revenue",
                "event_type": "order_cancelled",
                "amount_signed": "-1",
                "currency": "BRL",
            },
            {
                "event_class": "lifecycle",
                "event_type": "lead_created",
                "reverses_business_event_key": "some-event",
            },
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(
                BusinessEventDTOValidationError
            ):
                self._dto(**values)

    def test_posting_counterevents_require_exact_direction_and_target(self):
        for event_type, amount in (
            ("invoice_posting_reversed", "-100"),
            ("credit_note_posting_reversed", "100"),
        ):
            with self.subTest(event_type=event_type):
                values = dict(
                    event_class="revenue",
                    event_type=event_type,
                    amount_signed=amount,
                    currency="BRL",
                )
                with self.assertRaises(BusinessEventDTOValidationError):
                    self._dto(**values)
                dto = self._dto(
                    reverses_business_event_key="original-posting", **values
                )
                self.assertEqual(dto.event_type, event_type)
                values["amount_signed"] = str(-decimal.Decimal(amount))
                with self.assertRaises(BusinessEventDTOValidationError):
                    self._dto(reverses_business_event_key="original-posting", **values)

    def test_episode_answer_is_distinct_from_conversation_first_response(self):
        dto = self._dto(event_type="response_episode_answered")
        self.assertEqual(dto.event_class, "lifecycle")

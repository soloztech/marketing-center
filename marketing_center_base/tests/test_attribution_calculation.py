import datetime
import uuid

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from ..services.attribution_calculation_dto import (
    AttributionCalculationDTOValidationError,
    AttributionCandidateDTO,
    AttributionModelDTO,
)
from ..services.business_event_dto import MarketingBusinessEventDTO
from ..services.dto import MarketingTouchpointDTO
from ..services.tokens import MARKETING_ATTRIBUTION_CALCULATION_CAPABILITY_TOKEN


class TestMarketingAttributionCalculation(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.calculation_service = cls.env[
            "marketing.attribution.calculation.service"
        ].with_context(
            marketing_attribution_calculation_capability_token=(
                MARKETING_ATTRIBUTION_CALCULATION_CAPABILITY_TOKEN
            ),
            marketing_attribution_producer_key="marketing_center_test.fixture",
        )
        cls.touchpoint_service = cls.env["marketing.attribution.service"]
        cls.business_event_service = cls.env["marketing.business.event.service"]
        cls.subject_time = datetime.datetime(2026, 9, 1, 18, 0)

    def _unique(self, prefix):
        return "%s:%s" % (prefix, uuid.uuid4())

    def _model(self, strategy="linear", bases=None, **overrides):
        bases = bases or (
            ("provider_reported",)
            if strategy == "platform_reported"
            else ("deterministic_first_party",)
        )
        values = {
            "code": self._unique("model").replace(":", ".")[:64],
            "version": 1,
            "name": "Test attribution model",
            "strategy": strategy,
            "window_days": 30,
            "policy_ref": "test.policy.v1",
            "eligible_evidence_bases": bases,
        }
        values.update(overrides)
        registered = self.calculation_service._register_model(self.env.company, values)
        return self.env["marketing.attribution.model"].browse(registered.model_id)

    def _touchpoint(self, occurred_at, **overrides):
        occurrence = overrides.pop("source_occurrence_ref", self._unique("touchpoint"))
        values = {
            "source_system": "test.attribution",
            "source_scope_ref": "test-suite",
            "source_occurrence_ref": occurrence,
            "source_evidence_ref": self._unique("evidence"),
            "occurred_at": occurred_at,
            "platform": "web",
            "channel": "website",
            "touchpoint_type": "entry_point",
            "evidence_level": "first_party",
        }
        values.update(overrides)
        dto = MarketingTouchpointDTO(**values)
        result = self.touchpoint_service._ingest_touchpoint(self.env.company, dto)
        return self.env["marketing.attribution.touchpoint"].browse(result.touchpoint_id)

    def _event(self, amount="10.000001", **overrides):
        key = self._unique("business-event")
        values = {
            "event_class": "revenue",
            "event_type": "order_confirmed",
            "source_system": "test.sales",
            "source_model": "sale.order",
            "source_res_id": (uuid.uuid4().int % 1_000_000) + 1,
            "source_occurrence_ref": key,
            "source_evidence_ref": self._unique("event-evidence"),
            "business_event_key": key,
            "occurred_at": self.subject_time,
            "evidence_level": "first_party",
            "amount_signed": amount,
            "currency": "BRL",
        }
        values.update(overrides)
        result = self.business_event_service._ingest_event(
            self.env.company, MarketingBusinessEventDTO.from_dict(values)
        )
        return self.env["marketing.business.event"].browse(result.event_id)

    def _candidate(self, touchpoint, **overrides):
        values = {
            "touchpoint_id": touchpoint.id,
            "evidence_basis": "deterministic_first_party",
            "evidence_ref": self._unique("journey-link"),
            "channel_class": "paid",
        }
        values.update(overrides)
        return values

    def _calculate(self, model, event, candidates, calculation_ref=None, **overrides):
        values = {
            "calculation_ref": calculation_ref or self._unique("calculation"),
            "observed_at": self.subject_time,
            "candidates": candidates,
        }
        values.update(overrides)
        return self.calculation_service._calculate(
            self.env.company, model, event, values
        )

    def test_model_registration_is_versioned_idempotent_and_immutable(self):
        values = {
            "code": "test.first.touch",
            "version": 1,
            "name": "First touch",
            "strategy": "first_touch",
            "window_days": 30,
            "policy_ref": "policy.v1",
            "eligible_evidence_bases": ["deterministic_first_party"],
        }
        accepted = self.calculation_service._register_model(self.env.company, values)
        duplicate = self.calculation_service._register_model(self.env.company, values)
        self.assertEqual(accepted.model_id, duplicate.model_id)
        self.assertEqual(duplicate.disposition, "duplicate")
        with self.assertRaises(ValidationError):
            self.calculation_service._register_model(
                self.env.company, dict(values, window_days=60)
            )
        model = self.env["marketing.attribution.model"].browse(accepted.model_id)
        with self.assertRaises(AccessError):
            model.write({"window_days": 7})
        with self.assertRaises(AccessError):
            model.unlink()

    def test_correlation_cannot_be_configured_as_creditable(self):
        with self.assertRaises(AttributionCalculationDTOValidationError):
            AttributionModelDTO(
                code="test.correlation",
                version=1,
                name="Invalid correlation model",
                strategy="linear",
                window_days=30,
                policy_ref="policy.v1",
                eligible_evidence_bases=("correlation_only",),
            )
        with self.assertRaises(AttributionCalculationDTOValidationError):
            AttributionModelDTO(
                code="test.provider.mix",
                version=1,
                name="Invalid provider mix",
                strategy="linear",
                window_days=30,
                policy_ref="policy.v1",
                eligible_evidence_bases=("provider_reported",),
            )
        with self.assertRaises(AttributionCalculationDTOValidationError):
            AttributionModelDTO(
                code="test.manual.gate",
                version=1,
                name="Manual review without authority ledger",
                strategy="linear",
                window_days=30,
                policy_ref="policy.v1",
                eligible_evidence_bases=("manual_reviewed",),
            )

    def test_calculation_requires_internal_producer_capability(self):
        with self.assertRaises(AccessError):
            self.env["marketing.attribution.calculation.service"]._register_model(
                self.env.company,
                {
                    "code": "test.untrusted",
                    "version": 1,
                    "name": "Untrusted",
                    "strategy": "first_touch",
                    "window_days": 30,
                    "policy_ref": "policy.v1",
                    "eligible_evidence_bases": ["deterministic_first_party"],
                },
            )
        with self.assertRaises(AttributionCalculationDTOValidationError):
            AttributionCandidateDTO(
                touchpoint_id=1,
                evidence_basis="deterministic_first_party",
                evidence_ref="free form text from an operator",
            )

    def test_first_touch_credits_only_the_earliest_eligible_candidate(self):
        model = self._model(strategy="first_touch")
        first = self._touchpoint(self.subject_time - datetime.timedelta(days=3))
        second = self._touchpoint(self.subject_time - datetime.timedelta(days=1))
        event = self._event()
        calculation = self._calculate(
            model,
            event,
            [self._candidate(second), self._candidate(first)],
        )
        result = self.env["marketing.attribution.result"].browse(calculation.result_id)
        self.assertEqual(result.disposition, "credited")
        self.assertEqual(result.total_weight_micros, 1_000_000)
        self.assertEqual(result.contribution_ids.touchpoint_id, first)
        self.assertEqual(result.contribution_ids.weight_micros, 1_000_000)
        self.assertEqual(result.attributed_amount_micros, 10_000_001)
        self.assertEqual(
            second,
            result.candidate_ids.filtered(
                lambda item: item.state == "eligible_not_selected"
            ).touchpoint_id,
        )

    def test_linear_weights_and_amount_allocation_are_exact(self):
        model = self._model(strategy="linear")
        touchpoints = [
            self._touchpoint(self.subject_time - datetime.timedelta(days=offset))
            for offset in (3, 2, 1)
        ]
        event = self._event(amount="10.000001")
        calculation = self._calculate(
            model, event, [self._candidate(item) for item in touchpoints]
        )
        contributions = (
            self.env["marketing.attribution.result"]
            .browse(calculation.result_id)
            .contribution_ids
        )
        self.assertEqual(len(contributions), 3)
        self.assertEqual(sum(contributions.mapped("weight_micros")), 1_000_000)
        self.assertEqual(
            sum(contributions.mapped("allocated_amount_micros")), 10_000_001
        )
        self.assertEqual(
            sorted(contributions.mapped("weight_micros")),
            [333_333, 333_333, 333_334],
        )
        negative_allocations = self.calculation_service._allocate_amounts(
            -10_000_001, [333_334, 333_333, 333_333]
        )
        self.assertEqual(sum(negative_allocations), -10_000_001)
        self.assertTrue(all(value < 0 for value in negative_allocations))

    def test_correlation_is_preserved_but_result_is_unattributed(self):
        model = self._model(strategy="last_touch")
        touchpoint = self._touchpoint(self.subject_time - datetime.timedelta(hours=1))
        reviewed = self._touchpoint(self.subject_time - datetime.timedelta(hours=2))
        event = self._event()
        calculation = self._calculate(
            model,
            event,
            [
                self._candidate(
                    touchpoint,
                    evidence_basis="correlation_only",
                    channel_class="unknown",
                ),
                self._candidate(
                    reviewed,
                    evidence_basis="manual_reviewed",
                    evidence_ref=self._unique("review.decision"),
                    channel_class="unknown",
                ),
            ],
        )
        result = self.env["marketing.attribution.result"].browse(calculation.result_id)
        self.assertEqual(result.disposition, "unattributed")
        self.assertEqual(result.unattributed_reason, "no_eligible_candidates")
        self.assertEqual(
            set(result.candidate_ids.mapped("state")),
            {"excluded_correlation", "excluded_policy"},
        )
        self.assertFalse(result.contribution_ids)
        self.assertEqual(result.attributed_amount_micros, 0)

    def test_stale_revision_and_out_of_window_candidates_are_not_credited(self):
        occurrence = self._unique("revised-touchpoint")
        original = self._touchpoint(
            self.subject_time - datetime.timedelta(days=2),
            source_occurrence_ref=occurrence,
        )
        current = self._touchpoint(
            self.subject_time - datetime.timedelta(days=2),
            source_occurrence_ref=occurrence,
            revision_kind="enrichment",
            asset_refs={"google.campaign_id": "123"},
        )
        outside = self._touchpoint(self.subject_time - datetime.timedelta(days=90))
        model = self._model(strategy="linear")
        event = self._event()
        calculation = self._calculate(
            model,
            event,
            [self._candidate(original), self._candidate(outside)],
        )
        result = self.env["marketing.attribution.result"].browse(calculation.result_id)
        states = set(result.candidate_ids.mapped("state"))
        self.assertEqual(states, {"excluded_stale_revision", "excluded_window"})
        self.assertNotEqual(original, current)
        self.assertEqual(result.disposition, "unattributed")

    def test_platform_reported_is_separate_and_requires_exact_weights(self):
        model = self._model(strategy="platform_reported")
        first = self._touchpoint(self.subject_time - datetime.timedelta(days=2))
        second = self._touchpoint(self.subject_time - datetime.timedelta(days=1))
        event = self._event()
        candidates = [
            self._candidate(
                first,
                evidence_basis="provider_reported",
                reported_weight_micros=700_000,
            ),
            self._candidate(
                second,
                evidence_basis="provider_reported",
                reported_weight_micros=300_000,
            ),
        ]
        calculation = self._calculate(model, event, candidates)
        result = self.env["marketing.attribution.result"].browse(calculation.result_id)
        self.assertEqual(result.run_id.interpretation, "provider_reported")
        self.assertEqual(
            sorted(result.contribution_ids.mapped("weight_micros")),
            [300_000, 700_000],
        )

        invalid_candidates = [dict(item) for item in candidates]
        invalid_candidates[1]["reported_weight_micros"] = 200_000
        with self.assertRaises(ValidationError):
            self._calculate(model, event, invalid_candidates)

    def test_zero_weight_provider_candidate_is_audited_not_credited(self):
        model = self._model(strategy="platform_reported")
        zero = self._touchpoint(self.subject_time - datetime.timedelta(days=2))
        credited = self._touchpoint(self.subject_time - datetime.timedelta(days=1))
        event = self._event()
        calculation = self._calculate(
            model,
            event,
            [
                self._candidate(
                    zero,
                    evidence_basis="provider_reported",
                    reported_weight_micros=0,
                ),
                self._candidate(
                    credited,
                    evidence_basis="provider_reported",
                    reported_weight_micros=1_000_000,
                ),
            ],
        )
        result = self.env["marketing.attribution.result"].browse(calculation.result_id)
        zero_candidate = result.candidate_ids.filtered(
            lambda item: item.touchpoint_id == zero
        )
        self.assertEqual(zero_candidate.state, "eligible_not_selected")
        self.assertFalse(
            result.contribution_ids.filtered(lambda item: item.touchpoint_id == zero)
        )
        self.assertEqual(result.total_weight_micros, 1_000_000)
        self.assertEqual(result.credited_count, 1)

    def test_a_b_a_recalculation_preserves_history(self):
        model = self._model(strategy="last_touch")
        first = self._touchpoint(self.subject_time - datetime.timedelta(days=2))
        second = self._touchpoint(self.subject_time - datetime.timedelta(days=1))
        event = self._event()
        first_result = self._calculate(
            model,
            event,
            [self._candidate(first)],
            calculation_ref=self._unique("run-a1"),
        )
        second_result = self._calculate(
            model,
            event,
            [self._candidate(second)],
            calculation_ref=self._unique("run-b"),
        )
        third_ref = self._unique("run-a2")
        third_payload = [self._candidate(first)]
        third_result = self._calculate(
            model,
            event,
            third_payload,
            calculation_ref=third_ref,
        )
        replay = self._calculate(
            model,
            event,
            third_payload,
            calculation_ref=third_ref,
        )
        results = self.env["marketing.attribution.result"].browse(
            [first_result.result_id, second_result.result_id, third_result.result_id]
        )
        self.assertEqual(len(results), 3)
        self.assertEqual(
            results.mapped("contribution_ids").mapped("touchpoint_id"),
            first | second,
        )
        self.assertEqual(replay.result_id, third_result.result_id)
        self.assertEqual(replay.ingestion_disposition, "duplicate")

    def test_replay_is_idempotent_and_conflicting_reference_is_rejected(self):
        model = self._model(strategy="last_touch")
        touchpoint = self._touchpoint(self.subject_time - datetime.timedelta(hours=1))
        event = self._event()
        calculation_ref = self._unique("stable-calculation")
        payload = [self._candidate(touchpoint)]
        accepted = self._calculate(
            model, event, payload, calculation_ref=calculation_ref
        )
        duplicate = self._calculate(
            model, event, payload, calculation_ref=calculation_ref
        )
        self.assertEqual(accepted.run_id, duplicate.run_id)
        self.assertEqual(duplicate.ingestion_disposition, "duplicate")
        with self.assertRaises(ValidationError):
            self._calculate(model, event, [], calculation_ref=calculation_ref)

    def test_cross_company_candidate_is_rejected(self):
        other_company = self.env["res.company"].create({"name": "Attribution Other"})
        other_touchpoint = self.touchpoint_service.with_context(
            allowed_company_ids=[self.env.company.id, other_company.id]
        )._ingest_touchpoint(
            other_company,
            MarketingTouchpointDTO(
                source_system="test.other",
                source_scope_ref="other-suite",
                source_occurrence_ref=self._unique("other-touchpoint"),
                occurred_at=self.subject_time - datetime.timedelta(days=1),
                platform="web",
                channel="website",
                touchpoint_type="entry_point",
                evidence_level="first_party",
            ),
        )
        model = self._model(strategy="first_touch")
        event = self._event()
        with self.assertRaises(AccessError):
            self._calculate(
                model,
                event,
                [
                    {
                        "touchpoint_id": other_touchpoint.touchpoint_id,
                        "evidence_basis": "deterministic_first_party",
                        "evidence_ref": self._unique("cross-company"),
                    }
                ],
            )

    def test_calculation_ledgers_are_service_only(self):
        for model_name in (
            "marketing.attribution.model",
            "marketing.attribution.calculation.run",
            "marketing.attribution.result",
            "marketing.attribution.candidate",
            "marketing.attribution.contribution",
        ):
            with self.assertRaises(AccessError):
                self.env[model_name].sudo().create({})

        model = self._model(strategy="last_touch")
        touchpoint = self._touchpoint(self.subject_time - datetime.timedelta(hours=1))
        result_data = self._calculate(
            model, self._event(), [self._candidate(touchpoint)]
        )
        result = self.env["marketing.attribution.result"].browse(result_data.result_id)
        with self.assertRaises(AccessError):
            result.sudo().write({"disposition": "unattributed"})
        with self.assertRaises(AccessError):
            result.sudo().unlink()

    def test_acl_and_company_rules_protect_detailed_results(self):
        model = self._model(strategy="last_touch")
        touchpoint = self._touchpoint(self.subject_time - datetime.timedelta(hours=1))
        result_data = self._calculate(
            model, self._event(), [self._candidate(touchpoint)]
        )
        result = self.env["marketing.attribution.result"].browse(result_data.result_id)
        viewer = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Attribution Viewer",
                    "login": self._unique("attribution-viewer"),
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [
                        (
                            6,
                            0,
                            self.env.ref(
                                "marketing_center_base.group_marketing_center_viewer"
                            ).ids,
                        )
                    ],
                }
            )
        )
        with self.assertRaises(AccessError):
            result.with_user(viewer).read(["subject_key"])

        other_company = self.env["res.company"].create(
            {"name": "Attribution ACL Other"}
        )
        allowed = [self.env.company.id, other_company.id]
        other_calculation_service = self.calculation_service.with_context(
            allowed_company_ids=allowed
        )
        other_model_data = other_calculation_service._register_model(
            other_company,
            {
                "code": "test.other.company",
                "version": 1,
                "name": "Other company model",
                "strategy": "first_touch",
                "window_days": 30,
                "policy_ref": "other.policy.v1",
                "eligible_evidence_bases": ["deterministic_first_party"],
            },
        )
        other_touchpoint_data = self.touchpoint_service.with_context(
            allowed_company_ids=allowed
        )._ingest_touchpoint(
            other_company,
            MarketingTouchpointDTO(
                source_system="test.other.acl",
                source_scope_ref="other-acl-suite",
                source_occurrence_ref=self._unique("other-acl-touchpoint"),
                occurred_at=self.subject_time - datetime.timedelta(days=1),
                platform="web",
                channel="website",
                touchpoint_type="entry_point",
                evidence_level="first_party",
            ),
        )
        other_event_data = self.business_event_service.with_context(
            allowed_company_ids=allowed
        )._ingest_event(
            other_company,
            MarketingBusinessEventDTO.from_dict(
                {
                    "event_class": "lifecycle",
                    "event_type": "lead_created",
                    "source_system": "test.other.crm",
                    "source_model": "crm.lead",
                    "source_res_id": 1,
                    "source_occurrence_ref": self._unique("other-acl-event"),
                    "business_event_key": self._unique("other-acl-key"),
                    "occurred_at": self.subject_time,
                    "evidence_level": "first_party",
                }
            ),
        )
        other_result_data = other_calculation_service._calculate(
            other_company,
            other_calculation_service.env["marketing.attribution.model"].browse(
                other_model_data.model_id
            ),
            other_calculation_service.env["marketing.business.event"].browse(
                other_event_data.event_id
            ),
            {
                "calculation_ref": self._unique("other-acl-calculation"),
                "observed_at": self.subject_time,
                "candidates": [
                    {
                        "touchpoint_id": other_touchpoint_data.touchpoint_id,
                        "evidence_basis": "deterministic_first_party",
                        "evidence_ref": self._unique("other.journey"),
                    }
                ],
            },
        )
        admin = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Attribution Company Admin",
                    "login": self._unique("attribution-company-admin"),
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [
                        (
                            6,
                            0,
                            self.env.ref(
                                "marketing_center_base.group_marketing_center_admin"
                            ).ids,
                        )
                    ],
                }
            )
        )
        other_result = self.env["marketing.attribution.result"].browse(
            other_result_data.result_id
        )
        with self.assertRaises(AccessError):
            other_result.with_user(admin).with_context(
                allowed_company_ids=self.env.company.ids
            ).read(["subject_key"])

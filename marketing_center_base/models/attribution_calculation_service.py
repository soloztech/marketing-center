import datetime

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.attribution_calculation_dto import (
    AttributionCalculationDTO,
    AttributionCalculationDTOValidationError,
    AttributionCalculationResult,
    AttributionModelDTO,
    AttributionModelRegistrationResult,
)
from ..services.dto import canonical_json, sha256_text
from ..services.serialization import acquire_advisory_xact_lock
from ..services.tokens import (
    MARKETING_ATTRIBUTION_CALCULATION_CAPABILITY_TOKEN,
    MARKETING_ATTRIBUTION_CALCULATION_WRITE_TOKEN,
)

_WRITE_CONTEXT = {
    "marketing_attribution_calculation_write_token": (
        MARKETING_ATTRIBUTION_CALCULATION_WRITE_TOKEN
    )
}


class MarketingAttributionCalculationService(models.AbstractModel):
    _name = "marketing.attribution.calculation.service"
    _description = "Marketing Attribution Calculation Service"

    @api.model
    def _register_model(self, company, payload):
        producer_key = self._validated_producer()
        company_id = self._company_id_for_lock(company)
        try:
            dto = (
                payload
                if isinstance(payload, AttributionModelDTO)
                else AttributionModelDTO.from_dict(payload)
            )
        except AttributionCalculationDTOValidationError as error:
            raise ValidationError(_("Invalid attribution model: %s") % error) from error

        self._advisory_lock("model", company_id, "%s:%s" % (dto.code, dto.version))
        company = self._validated_company(company)
        model_store = self.env["marketing.attribution.model"].sudo()
        existing = model_store.search(
            [
                ("company_id", "=", company.id),
                ("code", "=", dto.code),
                ("version", "=", dto.version),
            ],
            limit=1,
        )
        if existing:
            if existing.content_hash != dto.content_hash:
                raise ValidationError(
                    _(
                        "Attribution model %(code)s v%(version)s already exists "
                        "with different immutable content."
                    )
                    % {"code": dto.code, "version": dto.version}
                )
            return AttributionModelRegistrationResult(
                model_id=existing.id,
                disposition="duplicate",
                content_hash=existing.content_hash,
            )

        model = (
            model_store.with_company(company)
            .with_context(**_WRITE_CONTEXT)
            .create(
                {
                    "company_id": company.id,
                    "name": "%s (v%s)" % (dto.name, dto.version),
                    "code": dto.code,
                    "version": dto.version,
                    "schema_version": dto.schema_version,
                    "strategy": dto.strategy,
                    "interpretation": dto.interpretation,
                    "window_days": dto.window_days,
                    "policy_ref": dto.policy_ref,
                    "registered_by_producer": producer_key,
                    "eligible_evidence_bases_json": list(dto.eligible_evidence_bases),
                    "content_hash": dto.content_hash,
                    "created_at": fields.Datetime.now(),
                }
            )
        )
        return AttributionModelRegistrationResult(
            model_id=model.id,
            disposition="accepted",
            content_hash=model.content_hash,
        )

    @api.model
    def _calculate(self, company, model, business_event, payload):
        producer_key = self._validated_producer()
        company_id = self._company_id_for_lock(company)
        try:
            dto = (
                payload
                if isinstance(payload, AttributionCalculationDTO)
                else AttributionCalculationDTO.from_dict(payload)
            )
        except AttributionCalculationDTOValidationError as error:
            raise ValidationError(
                _("Invalid attribution calculation: %s") % error
            ) from error

        self._advisory_lock("run", company_id, dto.calculation_ref)
        company = self._validated_company(company)
        model = self._validated_scoped_record(
            company, model, "marketing.attribution.model", _("attribution model")
        )
        business_event = self._validated_scoped_record(
            company,
            business_event,
            "marketing.business.event",
            _("business event"),
        )
        window_end = dto.window_end or business_event.occurred_at
        window_start = dto.window_start or (
            window_end - datetime.timedelta(days=model.window_days)
        )
        if window_end > business_event.occurred_at:
            raise ValidationError(
                _("The attribution window cannot extend beyond the business event.")
            )

        input_payload = dto.canonical_payload(
            model_content_hash=model.content_hash,
            business_event_key=business_event.business_event_key,
            window_start=window_start,
            window_end=window_end,
        )
        input_payload["producer_key"] = producer_key
        input_hash = sha256_text(canonical_json(input_payload))
        run_store = self.env["marketing.attribution.calculation.run"].sudo()
        existing = run_store.search(
            [
                ("company_id", "=", company.id),
                ("calculation_ref", "=", dto.calculation_ref),
            ],
            limit=1,
        )
        if existing:
            if existing.input_hash != input_hash:
                raise ValidationError(
                    _(
                        "This calculation reference already exists with different "
                        "immutable input."
                    )
                )
            return self._result(existing, "duplicate")

        prepared = self._prepare_candidates(
            company,
            model,
            business_event,
            dto.candidates,
            window_start,
            window_end,
        )
        weights = self._weights(model.strategy, prepared)
        for item in prepared:
            item["weight_micros"] = weights.get(item["candidate_index"], 0)
            if item["weight_micros"]:
                item["state"] = "credited"
                item["reason"] = "selected_by_model"
            elif item["state"] == "eligible":
                item["state"] = "eligible_not_selected"
                item["reason"] = "not_selected_by_model"

        candidate_count = len(prepared)
        eligible_count = len(
            {
                item["touchpoint"].id
                for item in prepared
                if item["state"] in {"credited", "eligible_not_selected"}
            }
        )
        credited = [item for item in prepared if item["state"] == "credited"]
        credited_count = len(credited)
        credit_disposition = "credited" if credited else "unattributed"
        unattributed_reason = (
            "none"
            if credited
            else ("no_candidates" if not prepared else "no_eligible_candidates")
        )
        subject_amount = (
            int(business_event.amount_signed_micros) if business_event.has_amount else 0
        )
        allocations = self._allocate_amounts(
            subject_amount,
            [item["weight_micros"] for item in credited],
        )
        calculated_at = fields.Datetime.now()
        run = (
            run_store.with_company(company)
            .with_context(**_WRITE_CONTEXT)
            .create(
                {
                    "company_id": company.id,
                    "calculation_ref": dto.calculation_ref,
                    "schema_version": dto.schema_version,
                    "model_id": model.id,
                    "model_code": model.code,
                    "model_version": model.version,
                    "policy_ref": model.policy_ref,
                    "producer_key": producer_key,
                    "interpretation": model.interpretation,
                    "observed_at": dto.observed_at,
                    "calculated_at": calculated_at,
                    "input_hash": input_hash,
                }
            )
        )
        result = (
            self.env["marketing.attribution.result"]
            .sudo()
            .with_company(company)
            .with_context(**_WRITE_CONTEXT)
            .create(
                {
                    "run_id": run.id,
                    "business_event_id": business_event.id,
                    "subject_kind": "business_event",
                    "subject_key": business_event.public_ref,
                    "window_start": window_start,
                    "window_end": window_end,
                    "calculated_at": calculated_at,
                    "disposition": credit_disposition,
                    "unattributed_reason": unattributed_reason,
                    "candidate_count": candidate_count,
                    "eligible_count": eligible_count,
                    "credited_count": credited_count,
                    "total_weight_micros": sum(
                        item["weight_micros"] for item in credited
                    ),
                    "has_amount": business_event.has_amount,
                    "subject_amount_micros": subject_amount,
                    "attributed_amount_micros": subject_amount if credited else 0,
                    "currency_id": business_event.currency_id.id or False,
                }
            )
        )
        candidate_records = self._create_candidates(company, result, prepared)
        self._create_contributions(
            company, result, prepared, candidate_records, allocations
        )
        return AttributionCalculationResult(
            run_id=run.id,
            result_id=result.id,
            ingestion_disposition="accepted",
            credit_disposition=result.disposition,
            candidate_count=result.candidate_count,
            eligible_count=result.eligible_count,
            credited_count=result.credited_count,
        )

    @api.model
    def _prepare_candidates(
        self,
        company,
        model,
        business_event,
        candidates,
        window_start,
        window_end,
    ):
        ordered = sorted(
            candidates,
            key=lambda item: (
                item.touchpoint_id,
                item.evidence_basis,
                item.evidence_ref,
            ),
        )
        touchpoint_ids = {item.touchpoint_id for item in ordered}
        touchpoints = (
            self.env["marketing.attribution.touchpoint"]
            .sudo()
            .browse(sorted(touchpoint_ids))
        )
        existing = touchpoints.exists()
        missing = touchpoint_ids - set(existing.ids)
        if missing:
            raise ValidationError(
                _("Attribution candidates reference missing touchpoint records.")
            )
        foreign = existing.filtered(lambda item: item.company_id != company)
        if foreign:
            raise AccessError(
                _("Attribution candidates cannot cross company boundaries.")
            )
        touchpoint_by_id = {item.id: item for item in existing}
        effective_ids = set(
            self.env["marketing.attribution.effective.touchpoint"]
            .sudo()
            .search(
                [
                    ("company_id", "=", company.id),
                    ("touchpoint_id", "in", sorted(touchpoint_ids)),
                ]
            )
            .mapped("touchpoint_id")
            .ids
        )
        eligible_bases = set(model.eligible_evidence_bases_json or ())
        prepared = []
        for index, candidate in enumerate(ordered):
            touchpoint = touchpoint_by_id[candidate.touchpoint_id]
            state, reason = self._candidate_state(
                candidate,
                touchpoint,
                effective_ids,
                eligible_bases,
                business_event.occurred_at,
                window_start,
                window_end,
            )
            prepared.append(
                {
                    "candidate_index": index,
                    "dto": candidate,
                    "touchpoint": touchpoint,
                    "state": state,
                    "reason": reason,
                }
            )

        eligible_by_touchpoint = {}
        for item in prepared:
            if item["state"] != "eligible":
                continue
            touchpoint_id = item["touchpoint"].id
            if touchpoint_id in eligible_by_touchpoint:
                item["state"] = "duplicate_touchpoint"
                item["reason"] = "same_touchpoint_already_eligible"
            else:
                eligible_by_touchpoint[touchpoint_id] = item
        return prepared

    @api.model
    def _candidate_state(
        self,
        candidate,
        touchpoint,
        effective_ids,
        eligible_bases,
        subject_occurred_at,
        window_start,
        window_end,
    ):
        if candidate.evidence_basis == "correlation_only":
            return "excluded_correlation", "correlation_is_not_credit_evidence"
        if candidate.evidence_basis not in eligible_bases:
            return "excluded_policy", "evidence_basis_not_allowed_by_model"
        if touchpoint.id not in effective_ids:
            return "excluded_stale_revision", "not_current_effective_revision"
        if touchpoint.occurred_at > subject_occurred_at:
            return "excluded_after_subject", "touchpoint_after_business_event"
        if not window_start <= touchpoint.occurred_at <= window_end:
            return "excluded_window", "outside_model_window"
        return "eligible", "eligible_under_model_policy"

    @api.model
    def _weights(self, strategy, prepared):
        eligible = [item for item in prepared if item["state"] == "eligible"]
        if not eligible:
            return {}
        chronological = sorted(
            eligible,
            key=lambda item: (
                item["touchpoint"].occurred_at,
                item["touchpoint"].id,
                item["candidate_index"],
            ),
        )
        if strategy == "first_touch":
            return {chronological[0]["candidate_index"]: 1_000_000}
        if strategy == "last_touch":
            return {chronological[-1]["candidate_index"]: 1_000_000}
        if strategy == "linear":
            quotient, remainder = divmod(1_000_000, len(chronological))
            return {
                item["candidate_index"]: quotient + (position < remainder)
                for position, item in enumerate(chronological)
            }
        if strategy == "platform_reported":
            weights = {
                item["candidate_index"]: item["dto"].reported_weight_micros
                for item in chronological
                if item["dto"].reported_weight_micros
            }
            if sum(weights.values()) != 1_000_000:
                raise ValidationError(
                    _(
                        "Provider-reported candidate weights must total exactly "
                        "one million micros."
                    )
                )
            return weights
        raise ValidationError(_("The attribution strategy is not supported."))

    @api.model
    def _allocate_amounts(self, amount_micros, weights):
        if not weights:
            return []
        sign = -1 if amount_micros < 0 else 1
        absolute_amount = abs(amount_micros)
        allocations = [
            sign * ((absolute_amount * weight) // 1_000_000) for weight in weights
        ]
        remainder = amount_micros - sum(allocations)
        for position in range(abs(remainder)):
            allocations[position % len(allocations)] += 1 if remainder > 0 else -1
        return allocations

    @api.model
    def _create_candidates(self, company, result, prepared):
        if not prepared:
            return self.env["marketing.attribution.candidate"]
        values = []
        for sequence, item in enumerate(prepared, start=1):
            dto = item["dto"]
            candidate_key = sha256_text(canonical_json(dto.canonical_payload()))
            values.append(
                {
                    "result_id": result.id,
                    "touchpoint_id": item["touchpoint"].id,
                    "candidate_key": candidate_key,
                    "candidate_sequence": sequence,
                    "evidence_basis": dto.evidence_basis,
                    "evidence_ref": dto.evidence_ref,
                    "channel_class": dto.channel_class,
                    "reported_weight_micros": (
                        dto.reported_weight_micros
                        if dto.reported_weight_micros is not None
                        else False
                    ),
                    "touchpoint_occurred_at": item["touchpoint"].occurred_at,
                    "state": item["state"],
                    "reason": item["reason"],
                }
            )
        return (
            self.env["marketing.attribution.candidate"]
            .sudo()
            .with_company(company)
            .with_context(**_WRITE_CONTEXT)
            .create(values)
        )

    @api.model
    def _create_contributions(
        self, company, result, prepared, candidate_records, allocations
    ):
        credited = [item for item in prepared if item["state"] == "credited"]
        if not credited:
            return self.env["marketing.attribution.contribution"]
        candidate_by_sequence = {
            record.candidate_sequence: record for record in candidate_records
        }
        values = []
        for sequence, (item, allocation) in enumerate(
            zip(credited, allocations), start=1
        ):
            touchpoint = item["touchpoint"]
            candidate = candidate_by_sequence[item["candidate_index"] + 1]
            values.append(
                {
                    "result_id": result.id,
                    "candidate_id": candidate.id,
                    "touchpoint_id": touchpoint.id,
                    "contribution_sequence": sequence,
                    "weight_micros": item["weight_micros"],
                    "allocated_amount_micros": allocation,
                    "evidence_basis": item["dto"].evidence_basis,
                    "platform": touchpoint.platform,
                    "channel": touchpoint.channel,
                    "touchpoint_type": touchpoint.touchpoint_type,
                    "touchpoint_occurred_at": touchpoint.occurred_at,
                }
            )
        return (
            self.env["marketing.attribution.contribution"]
            .sudo()
            .with_company(company)
            .with_context(**_WRITE_CONTEXT)
            .create(values)
        )

    @api.model
    def _result(self, run, ingestion_disposition):
        result = run.result_ids[:1]
        if not result:
            raise ValidationError(_("The immutable calculation run is incomplete."))
        return AttributionCalculationResult(
            run_id=run.id,
            result_id=result.id,
            ingestion_disposition=ingestion_disposition,
            credit_disposition=result.disposition,
            candidate_count=result.candidate_count,
            eligible_count=result.eligible_count,
            credited_count=result.credited_count,
        )

    @api.model
    def _validated_company(self, company):
        if (
            not company
            or getattr(company, "_name", "") != "res.company"
            or len(company) != 1
        ):
            raise ValidationError(_("A single valid company is required."))
        company = company.exists()
        if not company:
            raise ValidationError(_("A single valid company is required."))
        if company not in self.env.companies:
            raise AccessError(
                _("The attribution operation belongs to another company.")
            )
        return company

    @api.model
    def _company_id_for_lock(self, company):
        if (
            not company
            or getattr(company, "_name", "") != "res.company"
            or len(company) != 1
        ):
            raise ValidationError(_("A single valid company is required."))
        return company.id

    @api.model
    def _validated_scoped_record(self, company, record, model_name, label):
        if not record or getattr(record, "_name", "") != model_name or len(record) != 1:
            raise ValidationError(_("A single valid %s is required.") % label)
        record = record.exists()
        if not record:
            raise ValidationError(_("A single valid %s is required.") % label)
        if record.company_id != company:
            raise AccessError(_("The %s belongs to another company.") % label)
        return record

    @api.model
    def _advisory_lock(self, namespace, company_id, reference):
        acquire_advisory_xact_lock(
            self.env.cr,
            "marketing_attribution_%s:%s:%s" % (namespace, company_id, reference),
            "Concurrent attribution calculation requires a fresh snapshot",
        )

    @api.model
    def _validated_producer(self):
        if (
            self.env.context.get("marketing_attribution_calculation_capability_token")
            is not MARKETING_ATTRIBUTION_CALCULATION_CAPABILITY_TOKEN
        ):
            raise AccessError(
                _(
                    "Attribution calculation is an internal trust boundary and "
                    "requires a registered producer capability."
                )
            )
        producer_key = self.env.context.get("marketing_attribution_producer_key")
        if (
            not isinstance(producer_key, str)
            or not producer_key
            or len(producer_key) > 128
            or any(character.isspace() for character in producer_key)
        ):
            raise AccessError(_("A stable attribution producer key is required."))
        return producer_key

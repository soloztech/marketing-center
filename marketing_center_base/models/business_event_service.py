from odoo import _, api, models
from odoo.exceptions import AccessError, ValidationError

from ..services.business_event_dto import (
    REQUIRED_REVERSAL_EVENT_PAIRS,
    REVERSAL_EVENT_PAIRS,
    BusinessEventDTOValidationError,
    BusinessEventIngestResult,
    MarketingBusinessEventDTO,
)
from ..services.serialization import acquire_advisory_xact_lock
from ..services.tokens import MARKETING_BUSINESS_EVENT_WRITE_TOKEN


class MarketingBusinessEventService(models.AbstractModel):
    _name = "marketing.business.event.service"
    _description = "Marketing Business Event Ingestion Service"

    @api.model
    def _ingest_event(self, company, payload):
        if (
            not company
            or getattr(company, "_name", "") != "res.company"
            or len(company) != 1
        ):
            raise ValidationError(_("A single valid company is required."))
        company_id = company.id
        try:
            dto = (
                payload
                if isinstance(payload, MarketingBusinessEventDTO)
                else MarketingBusinessEventDTO.from_dict(payload)
            )
        except BusinessEventDTOValidationError as error:
            raise ValidationError(
                _("Invalid marketing business event: %s") % error
            ) from error

        # Reversals with different event keys still mutate the same aggregate
        # invariant.  Claim that shared key first, before the per-event key and
        # before reading the original or any prior credit notes.
        if dto.reverses_business_event_key:
            acquire_advisory_xact_lock(
                self.env.cr,
                self._reversal_lock_key(company_id, dto),
                "Concurrent business-event reversal requires a fresh snapshot",
            )
        acquire_advisory_xact_lock(
            self.env.cr,
            "marketing_business_event:%s:%s" % (company_id, dto.canonical_key),
            "Concurrent business-event ingestion requires a fresh snapshot",
        )
        company = company.exists()
        if not company:
            raise ValidationError(_("A single valid company is required."))
        if company not in self.env.companies:
            raise AccessError(_("The marketing business event is not available."))
        event_model = self.env["marketing.business.event"].sudo()
        existing = event_model.search(
            [
                ("company_id", "=", company.id),
                ("source_system", "=", dto.source_system),
                ("business_event_key", "=", dto.business_event_key),
            ],
            limit=1,
        )
        if existing:
            disposition = (
                "duplicate" if existing.content_hash == dto.content_hash else "conflict"
            )
            observation = self._observe(existing, dto, disposition)
            return self._result(existing, observation, disposition)

        currency = self._currency(dto.currency)
        reversed_event = self._reversed_event(company, dto)
        # Keep an empty recordset, rather than a boolean, so the common create
        # payload can safely read ``.id`` for both root and reversal events.
        root_event = self.env["marketing.business.event"]
        if reversed_event:
            root_event = reversed_event.root_event_id or reversed_event
        event = (
            event_model.with_company(company)
            .with_context(
                marketing_business_event_write_token=(
                    MARKETING_BUSINESS_EVENT_WRITE_TOKEN
                )
            )
            .create(
                {
                    "company_id": company.id,
                    "schema_version": dto.schema_version,
                    "event_class": dto.event_class,
                    "event_type": dto.event_type,
                    "source_system": dto.source_system,
                    "source_model": dto.source_model,
                    "source_res_id": dto.source_res_id,
                    "source_occurrence_ref": dto.source_occurrence_ref,
                    "source_evidence_ref": (
                        dto.source_evidence_ref or dto.source_occurrence_ref
                    ),
                    "business_event_key": dto.business_event_key,
                    "occurred_at": dto.occurred_at,
                    "observed_at": dto.observed_at,
                    "evidence_level": dto.evidence_level,
                    "has_amount": dto.amount_signed is not None,
                    "amount_signed_micros": (
                        int(dto.amount_signed * 1_000_000)
                        if dto.amount_signed is not None
                        else 0
                    ),
                    "currency_id": currency.id or False,
                    "reverses_event_id": reversed_event.id or False,
                    "root_event_id": root_event.id or False,
                    "canonical_key": dto.canonical_key,
                    "content_hash": dto.content_hash,
                    "snapshot_json": dto.canonical_content(),
                }
            )
        )
        observation = self._observe(event, dto, "accepted")
        return self._result(event, observation, "accepted")

    @api.model
    def _currency(self, currency_code):
        if not currency_code:
            return self.env["res.currency"]
        currency = (
            self.env["res.currency"]
            .sudo()
            .with_context(active_test=False)
            .search([("name", "=", currency_code)], limit=2)
        )
        if len(currency) != 1:
            raise ValidationError(_("The business-event currency is unavailable."))
        return currency

    @api.model
    def _reversal_lock_key(self, company_id, dto):
        return "marketing_business_event_reversal:%s:%s:%s" % (
            company_id,
            dto.source_system,
            dto.reverses_business_event_key,
        )

    @api.model
    def _reversed_event(self, company, dto):
        if not dto.reverses_business_event_key:
            return self.env["marketing.business.event"]
        event = (
            self.env["marketing.business.event"]
            .sudo()
            .search(
                [
                    ("company_id", "=", company.id),
                    ("source_system", "=", dto.source_system),
                    ("business_event_key", "=", dto.reverses_business_event_key),
                ],
                limit=1,
            )
        )
        if not event:
            raise ValidationError(_("The reversed business event does not exist."))
        if event.reverses_event_id:
            raise ValidationError(_("A reversal cannot reverse another reversal."))
        if event.event_class != dto.event_class:
            raise ValidationError(_("A reversal must preserve the event class."))
        expected_type = REVERSAL_EVENT_PAIRS.get(dto.event_type)
        if event.event_type != expected_type:
            raise ValidationError(
                _("The reversal does not match the original business-event type.")
            )
        if event.currency_id.name != dto.currency:
            raise ValidationError(
                _("A reversal or credit must preserve the original currency.")
            )
        original_micros = event.amount_signed_micros
        observed_micros = int(dto.amount_signed * 1_000_000)
        if dto.event_type in REQUIRED_REVERSAL_EVENT_PAIRS:
            if observed_micros != -original_micros:
                raise ValidationError(
                    _("A required reversal must exactly negate the original amount.")
                )
        elif observed_micros * original_micros > 0:
            raise ValidationError(_("A credit must oppose the original amount."))
        elif dto.event_type == "credit_note_posted":
            existing_credits = (
                self.env["marketing.business.event"]
                .sudo()
                .search(
                    [
                        ("reverses_event_id", "=", event.id),
                        ("event_type", "=", "credit_note_posted"),
                    ]
                )
            )
            credited_micros = (
                sum(existing_credits.mapped("amount_signed_micros")) + observed_micros
            )
            if abs(credited_micros) > abs(original_micros):
                raise ValidationError(
                    _(
                        "The cumulative credit amount cannot exceed the original "
                        "invoice amount."
                    )
                )
        if dto.event_type in REQUIRED_REVERSAL_EVENT_PAIRS:
            existing_reversal = (
                self.env["marketing.business.event"]
                .sudo()
                .search(
                    [("reverses_event_id", "=", event.id)],
                    limit=1,
                )
            )
            if existing_reversal:
                raise ValidationError(
                    _("The business event already has its required reversal.")
                )
        return event

    @api.model
    def _observe(self, event, dto, disposition):
        evidence_ref = dto.source_evidence_ref or dto.source_occurrence_ref
        observation_model = self.env["marketing.business.event.observation"].sudo()
        existing = observation_model.search(
            [
                ("event_id", "=", event.id),
                ("source_evidence_ref", "=", evidence_ref),
                ("observed_content_hash", "=", dto.content_hash),
            ],
            limit=1,
        )
        if existing:
            return existing
        return (
            observation_model.with_company(event.company_id)
            .with_context(
                marketing_business_event_write_token=(
                    MARKETING_BUSINESS_EVENT_WRITE_TOKEN
                )
            )
            .create(
                {
                    "event_id": event.id,
                    "observed_at": dto.observed_at,
                    "source_evidence_ref": evidence_ref,
                    "observed_content_hash": dto.content_hash,
                    "disposition": disposition,
                    "snapshot_json": dto.canonical_content(),
                }
            )
        )

    @api.model
    def _result(self, event, observation, disposition):
        return BusinessEventIngestResult(
            event_id=event.id,
            observation_id=observation.id,
            public_ref=event.public_ref,
            disposition=disposition,
            canonical_key=event.canonical_key,
            content_hash=observation.observed_content_hash,
        )

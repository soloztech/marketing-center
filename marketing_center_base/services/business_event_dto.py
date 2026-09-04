import dataclasses
import datetime
import decimal
import re
from typing import Any, Dict, Mapping, Optional

from .dto import (
    EVIDENCE_LEVELS,
    AttributionDTOValidationError,
    _bounded_text,
    _datetime,
    _datetime_from_iso,
    _json_mapping,
    _serialize,
    _token,
    canonical_json,
    sha256_text,
)

BUSINESS_EVENT_SCHEMA_VERSION = 1
BUSINESS_EVENT_CLASSES = frozenset({"lifecycle", "revenue"})
BUSINESS_EVENT_TYPES = frozenset(
    {
        "conversation_started",
        "credit_note_posted",
        "first_human_response",
        "invoice_posted",
        "interaction_started",
        "lead_created",
        "lead_stage_changed",
        "lost",
        "order_cancelled",
        "order_confirmed",
        "payment_allocated",
        "payment_allocation_reversed",
        "proposal_sent",
        "qualified",
        "won",
    }
)
BUSINESS_EVENT_TYPES_BY_CLASS = {
    "lifecycle": frozenset(
        {
            "conversation_started",
            "first_human_response",
            "interaction_started",
            "lead_created",
            "lead_stage_changed",
            "lost",
            "proposal_sent",
            "qualified",
            "won",
        }
    ),
    "revenue": frozenset(
        {
            "invoice_posted",
            "credit_note_posted",
            "order_cancelled",
            "order_confirmed",
            "payment_allocated",
            "payment_allocation_reversed",
        }
    ),
}
REQUIRED_REVERSAL_EVENT_PAIRS = {
    "order_cancelled": "order_confirmed",
    "payment_allocation_reversed": "payment_allocated",
}
OPTIONAL_REVERSAL_EVENT_PAIRS = {"credit_note_posted": "invoice_posted"}
REVERSAL_EVENT_PAIRS = {
    **REQUIRED_REVERSAL_EVENT_PAIRS,
    **OPTIONAL_REVERSAL_EVENT_PAIRS,
}
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_EXTENSION_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+$")
_MAX_ABS_AMOUNT = decimal.Decimal("999999999999999.999999")
_AMOUNT_QUANTUM = decimal.Decimal("0.000001")
_POSITIVE_REVENUE_TYPES = frozenset(
    {"invoice_posted", "order_confirmed", "payment_allocated"}
)
_NEGATIVE_REVENUE_TYPES = frozenset(
    {"credit_note_posted", "order_cancelled", "payment_allocation_reversed"}
)


class BusinessEventDTOValidationError(ValueError):
    """Raised when a provider-neutral business event is invalid."""


def _business_bounded_text(value, field_name, limit, required=False):
    try:
        return _bounded_text(value, field_name, limit, required=required)
    except AttributionDTOValidationError as error:
        raise BusinessEventDTOValidationError(str(error)) from error


def _business_token(value, field_name, required=True):
    try:
        return _token(value, field_name, required=required)
    except AttributionDTOValidationError as error:
        raise BusinessEventDTOValidationError(str(error)) from error


def _business_datetime(value, field_name):
    try:
        return _datetime(value, field_name)
    except AttributionDTOValidationError as error:
        raise BusinessEventDTOValidationError(str(error)) from error


def _business_json_mapping(value, field_name, **kwargs):
    try:
        return _json_mapping(value, field_name, **kwargs)
    except AttributionDTOValidationError as error:
        raise BusinessEventDTOValidationError(str(error)) from error


def _amount(value: Any) -> Optional[decimal.Decimal]:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise BusinessEventDTOValidationError(
            "amount_signed must use an exact decimal representation"
        )
    try:
        result = decimal.Decimal(value)
    except (decimal.InvalidOperation, TypeError, ValueError):
        raise BusinessEventDTOValidationError("amount_signed is invalid") from None
    if not result.is_finite() or abs(result) > _MAX_ABS_AMOUNT:
        raise BusinessEventDTOValidationError("amount_signed is invalid")
    if result != result.quantize(_AMOUNT_QUANTUM):
        raise BusinessEventDTOValidationError(
            "amount_signed supports at most six decimal places"
        )
    return result


def _amount_wire(value: Optional[decimal.Decimal]) -> str:
    if value is None:
        return ""
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _validated_amount_semantics(event_class, event_type, amount_value, currency_value):
    amount = _amount(amount_value)
    currency = _business_bounded_text(currency_value, "currency", 3).upper()
    if bool(amount is not None) != bool(currency):
        raise BusinessEventDTOValidationError(
            "currency is required exactly when amount_signed is present"
        )
    if currency and not _CURRENCY_RE.fullmatch(currency):
        raise BusinessEventDTOValidationError("currency is invalid")
    if event_class == "revenue" and amount is None:
        raise BusinessEventDTOValidationError(
            "revenue events require amount_signed and currency"
        )
    if event_class != "revenue" and amount is not None:
        raise BusinessEventDTOValidationError(
            "lifecycle events cannot carry a monetary amount"
        )
    if event_type in _POSITIVE_REVENUE_TYPES and amount < 0:
        raise BusinessEventDTOValidationError(
            "the revenue event requires a non-negative amount"
        )
    if event_type in _NEGATIVE_REVENUE_TYPES and amount > 0:
        raise BusinessEventDTOValidationError(
            "the reversal or credit event requires a non-positive amount"
        )
    return amount, currency


@dataclasses.dataclass(frozen=True)
class MarketingBusinessEventDTO:
    event_class: str
    event_type: str
    source_system: str
    source_model: str
    source_res_id: int
    source_occurrence_ref: str
    business_event_key: str
    occurred_at: datetime.datetime
    evidence_level: str
    observed_at: Optional[datetime.datetime] = None
    source_evidence_ref: str = ""
    amount_signed: Optional[decimal.Decimal] = None
    currency: str = ""
    reverses_business_event_key: str = ""
    extensions: Dict[str, Any] = dataclasses.field(default_factory=dict)
    schema_version: int = BUSINESS_EVENT_SCHEMA_VERSION

    def __post_init__(self):
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != BUSINESS_EVENT_SCHEMA_VERSION
        ):
            raise BusinessEventDTOValidationError(
                "unsupported MarketingBusinessEventDTO schema_version"
            )
        event_class = _business_token(self.event_class, "event_class")
        if event_class not in BUSINESS_EVENT_CLASSES:
            raise BusinessEventDTOValidationError("event_class is invalid")
        object.__setattr__(self, "event_class", event_class)
        event_type = _business_token(self.event_type, "event_type")
        if event_type not in BUSINESS_EVENT_TYPES_BY_CLASS[event_class]:
            raise BusinessEventDTOValidationError("event_type is invalid")
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(
            self,
            "source_system",
            _business_token(self.source_system, "source_system"),
        )
        object.__setattr__(
            self,
            "source_model",
            _business_token(self.source_model, "source_model"),
        )
        if (
            not isinstance(self.source_res_id, int)
            or isinstance(self.source_res_id, bool)
            or self.source_res_id <= 0
        ):
            raise BusinessEventDTOValidationError(
                "source_res_id must be a positive integer"
            )
        for field_name in (
            "source_occurrence_ref",
            "business_event_key",
        ):
            object.__setattr__(
                self,
                field_name,
                _business_bounded_text(
                    getattr(self, field_name), field_name, 512, required=True
                ),
            )
        object.__setattr__(
            self,
            "source_evidence_ref",
            _business_bounded_text(
                self.source_evidence_ref, "source_evidence_ref", 512
            ),
        )
        object.__setattr__(
            self,
            "occurred_at",
            _business_datetime(self.occurred_at, "occurred_at"),
        )
        object.__setattr__(
            self,
            "observed_at",
            _business_datetime(self.observed_at or self.occurred_at, "observed_at"),
        )
        evidence_level = _business_token(self.evidence_level, "evidence_level")
        if evidence_level not in EVIDENCE_LEVELS:
            raise BusinessEventDTOValidationError("evidence_level is invalid")
        object.__setattr__(self, "evidence_level", evidence_level)
        amount, currency = _validated_amount_semantics(
            event_class,
            event_type,
            self.amount_signed,
            self.currency,
        )
        object.__setattr__(self, "amount_signed", amount)
        object.__setattr__(self, "currency", currency)
        reversal = _business_bounded_text(
            self.reverses_business_event_key,
            "reverses_business_event_key",
            512,
        )
        if reversal and reversal == self.business_event_key:
            raise BusinessEventDTOValidationError("an event cannot reverse itself")
        if event_type in REQUIRED_REVERSAL_EVENT_PAIRS and not reversal:
            raise BusinessEventDTOValidationError(
                "reversal reference is required for the event type"
            )
        if reversal and event_type not in REVERSAL_EVENT_PAIRS:
            raise BusinessEventDTOValidationError(
                "reversal reference does not match the event type"
            )
        object.__setattr__(self, "reverses_business_event_key", reversal)
        extensions = _business_json_mapping(
            self.extensions,
            "extensions",
            namespaced_keys=True,
        )
        if any(not _EXTENSION_KEY_RE.fullmatch(key) for key in extensions):
            raise BusinessEventDTOValidationError(
                "extensions keys must use a namespace"
            )
        object.__setattr__(self, "extensions", extensions)

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]):
        try:
            payload = dict(values)
            for field_name in ("occurred_at", "observed_at"):
                if isinstance(payload.get(field_name), str):
                    payload[field_name] = _datetime_from_iso(payload[field_name])
            return cls(**payload)
        except BusinessEventDTOValidationError:
            raise
        except Exception as error:
            raise BusinessEventDTOValidationError(
                "invalid marketing business event payload"
            ) from error

    def canonical_identity(self):
        return {
            "version": self.schema_version,
            "source_system": self.source_system,
            "business_event_key": self.business_event_key,
        }

    def canonical_content(self):
        return {
            **self.canonical_identity(),
            "event_class": self.event_class,
            "event_type": self.event_type,
            "source_model": self.source_model,
            "source_res_id": self.source_res_id,
            "source_occurrence_ref": self.source_occurrence_ref,
            "occurred_at": self.occurred_at.isoformat(),
            "evidence_level": self.evidence_level,
            "amount_signed": _amount_wire(self.amount_signed),
            "currency": self.currency,
            "reverses_business_event_key": self.reverses_business_event_key,
            "extensions": self.extensions,
        }

    def to_dict(self):
        values = _serialize(self)
        values["amount_signed"] = _amount_wire(self.amount_signed)
        return values

    @property
    def canonical_key(self):
        return sha256_text(canonical_json(self.canonical_identity()))

    @property
    def content_hash(self):
        return sha256_text(canonical_json(self.canonical_content()))


@dataclasses.dataclass(frozen=True)
class BusinessEventIngestResult:
    event_id: int
    observation_id: int
    public_ref: str
    disposition: str
    canonical_key: str
    content_hash: str

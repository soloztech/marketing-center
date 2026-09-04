import datetime
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Tuple

from .dto import canonical_json, sha256_text

ATTRIBUTION_STRATEGIES = frozenset(
    {"first_touch", "last_touch", "linear", "platform_reported"}
)
DETERMINISTIC_EVIDENCE_BASES = frozenset({"deterministic_first_party"})
PENDING_AUTHORITY_EVIDENCE_BASES = frozenset({"manual_reviewed"})
PROVIDER_EVIDENCE_BASES = frozenset({"provider_reported"})
NON_CREDITABLE_EVIDENCE_BASES = frozenset({"correlation_only"})
ATTRIBUTION_EVIDENCE_BASES = (
    DETERMINISTIC_EVIDENCE_BASES
    | PENDING_AUTHORITY_EVIDENCE_BASES
    | PROVIDER_EVIDENCE_BASES
    | NON_CREDITABLE_EVIDENCE_BASES
)
CHANNEL_CLASSES = frozenset({"direct", "organic", "paid", "referral", "unknown"})

_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_REFERENCE_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,512}$")
_EVIDENCE_REFERENCE_RE = re.compile(
    r"^[a-z][a-z0-9_.-]{1,63}:[^\s\x00-\x1f\x7f]{1,447}$"
)


class AttributionCalculationDTOValidationError(ValueError):
    pass


@dataclass(frozen=True)
class AttributionModelRegistrationResult:
    model_id: int
    disposition: str
    content_hash: str


@dataclass(frozen=True)
class AttributionCalculationResult:
    run_id: int
    result_id: int
    ingestion_disposition: str
    credit_disposition: str
    candidate_count: int
    eligible_count: int
    credited_count: int


def _required_text(value: Any, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AttributionCalculationDTOValidationError(
            "%s must be a non-empty string" % field_name
        )
    value = value.strip()
    if len(value) > maximum or not _REFERENCE_RE.fullmatch(value):
        raise AttributionCalculationDTOValidationError("invalid %s" % field_name)
    return value


def _datetime(value: Any, field_name: str) -> datetime.datetime:
    if isinstance(value, str):
        try:
            value = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise AttributionCalculationDTOValidationError(
                "invalid %s" % field_name
            ) from error
    if not isinstance(value, datetime.datetime):
        raise AttributionCalculationDTOValidationError(
            "%s must be a datetime" % field_name
        )
    if value.tzinfo:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)


@dataclass(frozen=True)
class AttributionModelDTO:
    code: str
    version: int
    name: str
    strategy: str
    window_days: int
    policy_ref: str
    eligible_evidence_bases: Tuple[str, ...]
    schema_version: int = 1

    def __post_init__(self):
        code = _required_text(self.code, "code", 64).lower()
        if not _CODE_RE.fullmatch(code):
            raise AttributionCalculationDTOValidationError("invalid code")
        if not isinstance(self.version, int) or self.version < 1:
            raise AttributionCalculationDTOValidationError(
                "version must be a positive integer"
            )
        if not isinstance(self.schema_version, int) or self.schema_version < 1:
            raise AttributionCalculationDTOValidationError(
                "schema_version must be a positive integer"
            )
        name = _required_text(self.name, "name", 128)
        strategy = _required_text(self.strategy, "strategy", 32).lower()
        if strategy not in ATTRIBUTION_STRATEGIES:
            raise AttributionCalculationDTOValidationError("unsupported strategy")
        if not isinstance(self.window_days, int) or not 1 <= self.window_days <= 3650:
            raise AttributionCalculationDTOValidationError(
                "window_days must be between 1 and 3650"
            )
        policy_ref = _required_text(self.policy_ref, "policy_ref", 128)
        bases = tuple(sorted(set(self.eligible_evidence_bases or ())))
        if not bases or not set(bases).issubset(ATTRIBUTION_EVIDENCE_BASES):
            raise AttributionCalculationDTOValidationError(
                "eligible_evidence_bases contains an unsupported value"
            )
        if set(bases) & (
            NON_CREDITABLE_EVIDENCE_BASES | PENDING_AUTHORITY_EVIDENCE_BASES
        ):
            raise AttributionCalculationDTOValidationError(
                "correlation_only and manual_reviewed cannot currently be configured "
                "as creditable"
            )
        if strategy == "platform_reported":
            if set(bases) != PROVIDER_EVIDENCE_BASES:
                raise AttributionCalculationDTOValidationError(
                    "platform_reported requires provider_reported evidence only"
                )
        elif not set(bases).issubset(DETERMINISTIC_EVIDENCE_BASES):
            raise AttributionCalculationDTOValidationError(
                "journey models accept deterministic first-party evidence only until "
                "a review-authority ledger exists"
            )
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "strategy", strategy)
        object.__setattr__(self, "policy_ref", policy_ref)
        object.__setattr__(self, "eligible_evidence_bases", bases)

    @classmethod
    def from_dict(cls, values: Dict[str, Any]) -> "AttributionModelDTO":
        if not isinstance(values, dict):
            raise AttributionCalculationDTOValidationError(
                "model payload must be a dict"
            )
        return cls(
            code=values.get("code"),
            version=values.get("version"),
            name=values.get("name"),
            strategy=values.get("strategy"),
            window_days=values.get("window_days"),
            policy_ref=values.get("policy_ref"),
            eligible_evidence_bases=tuple(values.get("eligible_evidence_bases") or ()),
            schema_version=values.get("schema_version", 1),
        )

    @property
    def interpretation(self) -> str:
        return (
            "provider_reported"
            if self.strategy == "platform_reported"
            else "journey_credit"
        )

    def canonical_payload(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "code": self.code,
            "version": self.version,
            "name": self.name,
            "strategy": self.strategy,
            "window_days": self.window_days,
            "policy_ref": self.policy_ref,
            "eligible_evidence_bases": list(self.eligible_evidence_bases),
            "interpretation": self.interpretation,
        }

    @property
    def content_hash(self) -> str:
        return sha256_text(canonical_json(self.canonical_payload()))


@dataclass(frozen=True)
class AttributionCandidateDTO:
    touchpoint_id: int
    evidence_basis: str
    evidence_ref: str
    channel_class: str = "unknown"
    reported_weight_micros: Optional[int] = None

    def __post_init__(self):
        if not isinstance(self.touchpoint_id, int) or self.touchpoint_id < 1:
            raise AttributionCalculationDTOValidationError(
                "touchpoint_id must be a positive integer"
            )
        basis = _required_text(self.evidence_basis, "evidence_basis", 64).lower()
        if basis not in ATTRIBUTION_EVIDENCE_BASES:
            raise AttributionCalculationDTOValidationError("unsupported evidence_basis")
        evidence_ref = _required_text(self.evidence_ref, "evidence_ref", 512)
        if not _EVIDENCE_REFERENCE_RE.fullmatch(evidence_ref):
            raise AttributionCalculationDTOValidationError(
                "evidence_ref must be an immutable namespaced producer reference"
            )
        channel_class = _required_text(self.channel_class, "channel_class", 32).lower()
        if channel_class not in CHANNEL_CLASSES:
            raise AttributionCalculationDTOValidationError("unsupported channel_class")
        weight = self.reported_weight_micros
        if weight is not None and (
            not isinstance(weight, int) or not 0 <= weight <= 1_000_000
        ):
            raise AttributionCalculationDTOValidationError(
                "reported_weight_micros must be between 0 and 1000000"
            )
        if basis == "provider_reported" and weight is None:
            raise AttributionCalculationDTOValidationError(
                "provider_reported evidence requires reported_weight_micros"
            )
        if basis != "provider_reported" and weight is not None:
            raise AttributionCalculationDTOValidationError(
                "reported_weight_micros is valid only for provider_reported evidence"
            )
        object.__setattr__(self, "evidence_basis", basis)
        object.__setattr__(self, "evidence_ref", evidence_ref)
        object.__setattr__(self, "channel_class", channel_class)

    @classmethod
    def from_dict(cls, values: Dict[str, Any]) -> "AttributionCandidateDTO":
        if not isinstance(values, dict):
            raise AttributionCalculationDTOValidationError(
                "candidate payload must be a dict"
            )
        return cls(
            touchpoint_id=values.get("touchpoint_id"),
            evidence_basis=values.get("evidence_basis"),
            evidence_ref=values.get("evidence_ref"),
            channel_class=values.get("channel_class", "unknown"),
            reported_weight_micros=values.get("reported_weight_micros"),
        )

    def canonical_payload(self) -> Dict[str, Any]:
        return {
            "touchpoint_id": self.touchpoint_id,
            "evidence_basis": self.evidence_basis,
            "evidence_ref": self.evidence_ref,
            "channel_class": self.channel_class,
            "reported_weight_micros": self.reported_weight_micros,
        }


@dataclass(frozen=True)
class AttributionCalculationDTO:
    calculation_ref: str
    candidates: Tuple[AttributionCandidateDTO, ...]
    observed_at: datetime.datetime
    window_start: Optional[datetime.datetime] = None
    window_end: Optional[datetime.datetime] = None
    schema_version: int = 1

    def __post_init__(self):
        calculation_ref = _required_text(self.calculation_ref, "calculation_ref", 512)
        if not isinstance(self.schema_version, int) or self.schema_version < 1:
            raise AttributionCalculationDTOValidationError(
                "schema_version must be a positive integer"
            )
        observed_at = _datetime(self.observed_at, "observed_at")
        window_start = (
            _datetime(self.window_start, "window_start")
            if self.window_start is not None
            else None
        )
        window_end = (
            _datetime(self.window_end, "window_end")
            if self.window_end is not None
            else None
        )
        if bool(window_start) != bool(window_end):
            raise AttributionCalculationDTOValidationError(
                "window_start and window_end must be supplied together"
            )
        if window_start and window_start > window_end:
            raise AttributionCalculationDTOValidationError(
                "window_start cannot be after window_end"
            )
        candidates = tuple(self.candidates or ())
        if any(not isinstance(item, AttributionCandidateDTO) for item in candidates):
            raise AttributionCalculationDTOValidationError(
                "candidates must contain AttributionCandidateDTO values"
            )
        candidate_payloads = [
            canonical_json(item.canonical_payload()) for item in candidates
        ]
        if len(candidate_payloads) != len(set(candidate_payloads)):
            raise AttributionCalculationDTOValidationError(
                "duplicate candidate evidence is not allowed"
            )
        object.__setattr__(self, "calculation_ref", calculation_ref)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "window_start", window_start)
        object.__setattr__(self, "window_end", window_end)
        object.__setattr__(self, "candidates", candidates)

    @classmethod
    def from_dict(cls, values: Dict[str, Any]) -> "AttributionCalculationDTO":
        if not isinstance(values, dict):
            raise AttributionCalculationDTOValidationError(
                "calculation payload must be a dict"
            )
        return cls(
            calculation_ref=values.get("calculation_ref"),
            candidates=tuple(
                AttributionCandidateDTO.from_dict(item)
                for item in (values.get("candidates") or ())
            ),
            observed_at=values.get("observed_at"),
            window_start=values.get("window_start"),
            window_end=values.get("window_end"),
            schema_version=values.get("schema_version", 1),
        )

    def canonical_payload(
        self,
        *,
        model_content_hash: str,
        business_event_key: str,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> Dict[str, Any]:
        candidates: Iterable[Dict[str, Any]] = (
            candidate.canonical_payload() for candidate in self.candidates
        )
        return {
            "schema_version": self.schema_version,
            "calculation_ref": self.calculation_ref,
            "model_content_hash": model_content_hash,
            "business_event_key": business_event_key,
            "observed_at": self.observed_at.isoformat(),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "candidates": sorted(
                candidates,
                key=lambda item: (
                    item["touchpoint_id"],
                    item["evidence_basis"],
                    item["evidence_ref"],
                ),
            ),
        }

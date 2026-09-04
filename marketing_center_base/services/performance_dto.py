import dataclasses
import datetime
import re
from typing import Any, Dict, Mapping, Optional, Tuple

from .dto import canonical_json, sha256_text
from .timezone import LocalDateBoundaryError, local_date_boundary_utc

PERFORMANCE_METRIC_SCHEMA_VERSION = 1
PERFORMANCE_PAGE_SCHEMA_VERSION = 1

PERFORMANCE_GRAINS = frozenset({"account", "campaign", "ad_group", "ad", "keyword"})
PERFORMANCE_DIMENSIONS = frozenset(
    {
        "country",
        "device",
        "placement",
        "platform",
        "publisher_platform",
        "region",
    }
)
PERFORMANCE_METRIC_ORIGINS = frozenset({"platform_reported"})

_MAX_PAGE_ITEMS = 200
_MAX_PAGE_ERRORS = 200
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_ERROR_KEYS = frozenset({"code", "item_ref", "message", "retryable"})
EMPTY_REPORTING_CONTEXT_HASH = sha256_text(canonical_json({}))


class PerformanceDTOValidationError(ValueError):
    """Raised when a provider-neutral performance contract is invalid."""


def _bounded_text(value: Any, field_name: str, limit: int, required=False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise PerformanceDTOValidationError("%s must be text" % field_name)
    value = value.strip()
    if required and not value:
        raise PerformanceDTOValidationError("%s is required" % field_name)
    if len(value) > limit or any(ord(character) < 32 for character in value):
        raise PerformanceDTOValidationError("%s is invalid" % field_name)
    return value


def _choice(value: Any, field_name: str, choices) -> str:
    value = _bounded_text(value, field_name, 128, required=True).lower()
    if value not in choices:
        raise PerformanceDTOValidationError("%s is unsupported" % field_name)
    return value


def _schema_version(value: Any, expected: int, contract_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value != expected:
        raise PerformanceDTOValidationError(
            "unsupported %s schema_version" % contract_name
        )
    return value


def _date(value: Any, field_name: str) -> datetime.date:
    if isinstance(value, datetime.datetime) or not isinstance(value, datetime.date):
        raise PerformanceDTOValidationError("%s must be a date" % field_name)
    return value


def _date_from_iso(value: Any, field_name: str) -> datetime.date:
    if not isinstance(value, str):
        raise PerformanceDTOValidationError("%s must use ISO-8601" % field_name)
    try:
        return _date(datetime.date.fromisoformat(value), field_name)
    except ValueError as error:
        raise PerformanceDTOValidationError(
            "%s must use ISO-8601" % field_name
        ) from error


def _datetime(value: Any, field_name: str) -> datetime.datetime:
    if not isinstance(value, datetime.datetime):
        raise PerformanceDTOValidationError("%s must be a datetime" % field_name)
    if value.tzinfo:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)


def _datetime_from_iso(value: Any, field_name: str) -> datetime.datetime:
    if not isinstance(value, str):
        raise PerformanceDTOValidationError("%s must use ISO-8601" % field_name)
    try:
        value = "%s+00:00" % value[:-1] if value.endswith("Z") else value
        return _datetime(datetime.datetime.fromisoformat(value), field_name)
    except ValueError as error:
        raise PerformanceDTOValidationError(
            "%s must use ISO-8601" % field_name
        ) from error


def _counter(value: Any, field_name: str) -> Optional[int]:
    if value is None:
        return None
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value > 2**63 - 1
    ):
        raise PerformanceDTOValidationError(
            "%s must be a non-negative BIGINT" % field_name
        )
    return value


def _dimensions(value: Any) -> Dict[str, str]:
    if value in (None, False):
        return {}
    if not isinstance(value, Mapping):
        raise PerformanceDTOValidationError("dimensions must be an object")
    if set(value) - PERFORMANCE_DIMENSIONS:
        raise PerformanceDTOValidationError("dimensions contains unsupported keys")
    normalized = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise PerformanceDTOValidationError("dimensions keys must be text")
        normalized[key] = _bounded_text(
            item,
            "dimensions.%s" % key,
            256,
            required=True,
        )
    return dict(sorted(normalized.items()))


def _hash(value: Any, field_name: str) -> str:
    value = _bounded_text(value, field_name, 64, required=True).lower()
    if not _SHA256_RE.fullmatch(value):
        raise PerformanceDTOValidationError("%s must be a SHA-256 digest" % field_name)
    return value


def _error(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - _ERROR_KEYS:
        raise PerformanceDTOValidationError("errors contains unsupported keys")
    code = _bounded_text(value.get("code"), "errors.code", 128, required=True)
    item_ref = _bounded_text(value.get("item_ref"), "errors.item_ref", 1024)
    message = _bounded_text(value.get("message"), "errors.message", 1024)
    retryable = value.get("retryable", False)
    if not isinstance(retryable, bool):
        raise PerformanceDTOValidationError("errors.retryable must be a boolean")
    return {
        "code": code,
        "item_ref": item_ref,
        "message": message,
        "retryable": retryable,
    }


def _wire_datetime(value: datetime.datetime) -> str:
    return "%sZ" % value.isoformat()


def _serialize(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: _serialize(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, datetime.datetime):
        return _wire_datetime(value)
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    return value


def _expected_utc_bounds(report_date: datetime.date, timezone_name: str):
    try:
        local_start = local_date_boundary_utc(report_date, timezone_name)
        local_end = local_date_boundary_utc(
            report_date + datetime.timedelta(days=1), timezone_name
        )
    except (LocalDateBoundaryError, OverflowError) as error:
        raise PerformanceDTOValidationError(
            "report_timezone cannot define reproducible daily UTC bounds"
        ) from error
    return local_start, local_end


@dataclasses.dataclass(frozen=True)
class MarketingPerformanceDTO:
    grain: str
    entity_external_ref: str
    report_date: datetime.date
    period_start_utc: datetime.datetime
    period_end_utc: datetime.datetime
    report_timezone: str
    currency: str
    observed_at: datetime.datetime
    impressions: Optional[int] = None
    clicks: Optional[int] = None
    cost_micros: Optional[int] = None
    dimensions: Dict[str, str] = dataclasses.field(default_factory=dict)
    metric_origin: str = "platform_reported"
    reporting_context_hash: str = EMPTY_REPORTING_CONTEXT_HASH
    source_schema_version: str = ""
    schema_version: int = PERFORMANCE_METRIC_SCHEMA_VERSION

    def __post_init__(self):
        _schema_version(
            self.schema_version,
            PERFORMANCE_METRIC_SCHEMA_VERSION,
            "MarketingPerformanceDTO",
        )
        object.__setattr__(
            self, "grain", _choice(self.grain, "grain", PERFORMANCE_GRAINS)
        )
        object.__setattr__(
            self,
            "entity_external_ref",
            _bounded_text(
                self.entity_external_ref,
                "entity_external_ref",
                1024,
                required=True,
            ),
        )
        object.__setattr__(self, "report_date", _date(self.report_date, "report_date"))
        object.__setattr__(
            self,
            "period_start_utc",
            _datetime(self.period_start_utc, "period_start_utc"),
        )
        object.__setattr__(
            self,
            "period_end_utc",
            _datetime(self.period_end_utc, "period_end_utc"),
        )
        timezone_name = _bounded_text(
            self.report_timezone,
            "report_timezone",
            64,
            required=True,
        )
        expected_start, expected_end = _expected_utc_bounds(
            self.report_date,
            timezone_name,
        )
        if (
            self.period_start_utc != expected_start
            or self.period_end_utc != expected_end
        ):
            raise PerformanceDTOValidationError(
                "period UTC bounds do not match report_date and report_timezone"
            )
        object.__setattr__(self, "report_timezone", timezone_name)
        currency = _bounded_text(self.currency, "currency", 3, required=True).upper()
        if not _CURRENCY_RE.fullmatch(currency):
            raise PerformanceDTOValidationError("currency must be an ISO code")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(
            self, "observed_at", _datetime(self.observed_at, "observed_at")
        )
        object.__setattr__(
            self, "impressions", _counter(self.impressions, "impressions")
        )
        object.__setattr__(self, "clicks", _counter(self.clicks, "clicks"))
        object.__setattr__(
            self, "cost_micros", _counter(self.cost_micros, "cost_micros")
        )
        if (
            self.impressions is None
            and self.clicks is None
            and self.cost_micros is None
        ):
            raise PerformanceDTOValidationError(
                "at least one performance metric is required"
            )
        object.__setattr__(self, "dimensions", _dimensions(self.dimensions))
        object.__setattr__(
            self,
            "metric_origin",
            _choice(
                self.metric_origin,
                "metric_origin",
                PERFORMANCE_METRIC_ORIGINS,
            ),
        )
        object.__setattr__(
            self,
            "reporting_context_hash",
            _hash(self.reporting_context_hash, "reporting_context_hash"),
        )
        object.__setattr__(
            self,
            "source_schema_version",
            _bounded_text(self.source_schema_version, "source_schema_version", 128),
        )

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> "MarketingPerformanceDTO":
        if not isinstance(values, Mapping):
            raise PerformanceDTOValidationError("performance metric must be an object")
        try:
            payload = dict(values)
            if isinstance(payload.get("report_date"), str):
                payload["report_date"] = _date_from_iso(
                    payload["report_date"], "report_date"
                )
            for field_name in ("period_start_utc", "period_end_utc", "observed_at"):
                if isinstance(payload.get(field_name), str):
                    payload[field_name] = _datetime_from_iso(
                        payload[field_name], field_name
                    )
            return cls(**payload)
        except PerformanceDTOValidationError:
            raise
        except (TypeError, ValueError) as error:
            raise PerformanceDTOValidationError(
                "invalid performance metric payload"
            ) from error

    def to_dict(self) -> Dict[str, Any]:
        return _serialize(self)

    @property
    def dimension_hash(self) -> str:
        return sha256_text(canonical_json(self.dimensions))

    def canonical_identity(self) -> Dict[str, Any]:
        return {
            "version": self.schema_version,
            "grain": self.grain,
            "entity_external_ref": self.entity_external_ref,
            "report_date": self.report_date.isoformat(),
            "dimension_hash": self.dimension_hash,
            "metric_origin": self.metric_origin,
            "reporting_context_hash": self.reporting_context_hash,
        }

    def canonical_content(self) -> Dict[str, Any]:
        return {
            **self.canonical_identity(),
            "period_start_utc": _wire_datetime(self.period_start_utc),
            "period_end_utc": _wire_datetime(self.period_end_utc),
            "report_timezone": self.report_timezone,
            "currency": self.currency,
            "impressions": self.impressions,
            "clicks": self.clicks,
            "cost_micros": self.cost_micros,
            "dimensions": self.dimensions,
            "source_schema_version": self.source_schema_version,
        }

    @property
    def canonical_key(self) -> str:
        return sha256_text(canonical_json(self.canonical_identity()))

    @property
    def content_hash(self) -> str:
        return sha256_text(canonical_json(self.canonical_content()))


@dataclasses.dataclass(frozen=True)
class PerformancePageDTO:
    items: Tuple[MarketingPerformanceDTO, ...] = ()
    next_cursor: str = ""
    has_more: bool = False
    provider_request_id: str = ""
    provider_job_ref: str = ""
    provider_job_state: str = ""
    watermark: str = ""
    reporting_context_hash: str = EMPTY_REPORTING_CONTEXT_HASH
    retry_after: int = 0
    errors: Tuple[Dict[str, Any], ...] = ()
    authoritative_complete: bool = False
    schema_version: int = PERFORMANCE_PAGE_SCHEMA_VERSION

    def __post_init__(self):
        _schema_version(
            self.schema_version,
            PERFORMANCE_PAGE_SCHEMA_VERSION,
            "PerformancePageDTO",
        )
        items = tuple(
            (
                item
                if isinstance(item, MarketingPerformanceDTO)
                else MarketingPerformanceDTO.from_dict(item)
            )
            for item in tuple(self.items or ())
        )
        if len(items) > _MAX_PAGE_ITEMS:
            raise PerformanceDTOValidationError(
                "items exceeds the supported page limit"
            )
        identities = {item.canonical_key for item in items}
        if len(identities) != len(items):
            raise PerformanceDTOValidationError(
                "items contains duplicate metric identities"
            )
        object.__setattr__(self, "items", items)
        if not isinstance(self.has_more, bool):
            raise PerformanceDTOValidationError("has_more must be a boolean")
        object.__setattr__(
            self, "next_cursor", _bounded_text(self.next_cursor, "next_cursor", 4096)
        )
        object.__setattr__(
            self,
            "provider_request_id",
            _bounded_text(self.provider_request_id, "provider_request_id", 512),
        )
        object.__setattr__(
            self,
            "provider_job_ref",
            _bounded_text(self.provider_job_ref, "provider_job_ref", 1024),
        )
        object.__setattr__(
            self,
            "provider_job_state",
            _bounded_text(self.provider_job_state, "provider_job_state", 128),
        )
        if bool(self.provider_job_ref) != bool(self.provider_job_state):
            raise PerformanceDTOValidationError(
                "provider_job_ref and provider_job_state must be supplied together"
            )
        if self.has_more and not (self.next_cursor or self.provider_job_ref):
            raise PerformanceDTOValidationError(
                "has_more requires next_cursor or provider_job_ref"
            )
        object.__setattr__(
            self, "watermark", _bounded_text(self.watermark, "watermark", 4096)
        )
        object.__setattr__(
            self,
            "reporting_context_hash",
            _hash(self.reporting_context_hash, "reporting_context_hash"),
        )
        if (
            not isinstance(self.retry_after, int)
            or isinstance(self.retry_after, bool)
            or self.retry_after < 0
            or self.retry_after > 86400
        ):
            raise PerformanceDTOValidationError(
                "retry_after must be between 0 and 86400 seconds"
            )
        errors = tuple(_error(error) for error in tuple(self.errors or ()))
        if len(errors) > _MAX_PAGE_ERRORS:
            raise PerformanceDTOValidationError(
                "errors exceeds the supported page limit"
            )
        object.__setattr__(self, "errors", errors)
        if not isinstance(self.authoritative_complete, bool):
            raise PerformanceDTOValidationError(
                "authoritative_complete must be a boolean"
            )
        if self.authoritative_complete and self.has_more:
            raise PerformanceDTOValidationError(
                "an authoritative complete page cannot have more pages"
            )

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> "PerformancePageDTO":
        if not isinstance(values, Mapping):
            raise PerformanceDTOValidationError("performance page must be an object")
        try:
            payload = dict(values)
            payload["items"] = tuple(payload.get("items") or ())
            payload["errors"] = tuple(payload.get("errors") or ())
            return cls(**payload)
        except PerformanceDTOValidationError:
            raise
        except (TypeError, ValueError) as error:
            raise PerformanceDTOValidationError(
                "invalid performance page payload"
            ) from error

    def to_dict(self) -> Dict[str, Any]:
        return _serialize(self)


@dataclasses.dataclass(frozen=True)
class PerformanceIngestResult:
    metric_id: int
    disposition: str
    revision_sequence: int
    content_hash: str

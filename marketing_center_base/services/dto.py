import dataclasses
import datetime
import hashlib
import json
import re
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

MARKETING_TOUCHPOINT_SCHEMA_VERSION = 1

TOUCHPOINT_TYPES = frozenset(
    {
        "conversation_start",
        "entry_point",
        "form_submission",
        "lead_ad",
        "organic_link",
        "paid_ad_click",
        "paid_ad_signal",
        "unknown",
    }
)
EVIDENCE_LEVELS = frozenset(
    {
        "derived",
        "first_party",
        "imported",
        "observed",
        "provider_asserted",
        "provider_asserted_non_paid",
        "provider_hint",
    }
)
CONSENT_STATES = frozenset({"denied", "granted", "unknown"})
REVISION_KINDS = frozenset({"conflict", "correction", "enrichment", "observation"})
_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UTM_KEYS = frozenset({"campaign", "content", "medium", "source", "term"})
_MAX_JSON_BYTES = 16 * 1024


class AttributionDTOValidationError(ValueError):
    """Raised when a normalized marketing attribution contract is invalid."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_text(
    value: Any, field_name: str, limit: int, required: bool = False
) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise AttributionDTOValidationError("%s must be text" % field_name)
    normalized = value.strip()
    if required and not normalized:
        raise AttributionDTOValidationError("%s is required" % field_name)
    if len(normalized) > limit:
        raise AttributionDTOValidationError("%s is too long" % field_name)
    if any(ord(character) < 32 for character in normalized):
        raise AttributionDTOValidationError(
            "%s contains control characters" % field_name
        )
    return normalized


def _token(value: Any, field_name: str, required: bool = True) -> str:
    normalized = _bounded_text(value, field_name, 128, required=required).lower()
    if normalized and not _TOKEN_RE.fullmatch(normalized):
        raise AttributionDTOValidationError("%s has an invalid namespace" % field_name)
    return normalized


def _datetime(value: Any, field_name: str) -> datetime.datetime:
    if not isinstance(value, datetime.datetime):
        raise AttributionDTOValidationError("%s must be a datetime" % field_name)
    if value.tzinfo:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)


def _datetime_from_iso(value: str) -> datetime.datetime:
    if value.endswith("Z"):
        value = "%s+00:00" % value[:-1]
    return datetime.datetime.fromisoformat(value)


def sanitize_url(value: Any, field_name: str) -> str:
    """Return a conservative canonical URL without credentials, query or fragment."""

    normalized = _bounded_text(value, field_name, 2048)
    if not normalized:
        return ""
    try:
        parsed = urlsplit(normalized)
    except ValueError as error:
        raise AttributionDTOValidationError("%s is invalid" % field_name) from error
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise AttributionDTOValidationError("%s must be an HTTP(S) URL" % field_name)
    if parsed.username or parsed.password:
        raise AttributionDTOValidationError(
            "%s cannot contain credentials" % field_name
        )
    try:
        port = parsed.port
    except ValueError as error:
        raise AttributionDTOValidationError(
            "%s has an invalid port" % field_name
        ) from error
    hostname = parsed.hostname.lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = "[%s]" % hostname
    netloc = hostname
    if port is not None:
        netloc = "%s:%s" % (netloc, port)
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", "", ""))


def _json_mapping(
    value: Any,
    field_name: str,
    *,
    allowed_keys: Optional[frozenset] = None,
    namespaced_keys: bool = False,
) -> Dict[str, Any]:
    if value in (None, False):
        return {}
    if not isinstance(value, Mapping):
        raise AttributionDTOValidationError("%s must be an object" % field_name)
    normalized = dict(value)
    if allowed_keys is not None and set(normalized) - allowed_keys:
        raise AttributionDTOValidationError("%s contains unsupported keys" % field_name)
    for key in normalized:
        if not isinstance(key, str):
            raise AttributionDTOValidationError("%s keys must be text" % field_name)
        if namespaced_keys and not _TOKEN_RE.fullmatch(key.lower()):
            raise AttributionDTOValidationError(
                "%s contains an invalid key" % field_name
            )
    try:
        encoded = canonical_json(normalized).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise AttributionDTOValidationError(
            "%s must be JSON serializable" % field_name
        ) from error
    if len(encoded) > _MAX_JSON_BYTES:
        raise AttributionDTOValidationError("%s is too large" % field_name)
    return normalized


def _serialize(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: _serialize(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, tuple):
        return [_serialize(item) for item in value]
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    return value


@dataclasses.dataclass(frozen=True)
class PrivacySnapshotDTO:
    policy_version: str = ""
    notice_version: str = ""
    legal_basis_code: str = ""
    consent_state: str = "unknown"
    decision_source: str = ""
    decided_at: Optional[datetime.datetime] = None

    def __post_init__(self):
        object.__setattr__(
            self,
            "policy_version",
            _bounded_text(self.policy_version, "privacy.policy_version", 128),
        )
        object.__setattr__(
            self,
            "notice_version",
            _bounded_text(self.notice_version, "privacy.notice_version", 128),
        )
        object.__setattr__(
            self,
            "legal_basis_code",
            _token(self.legal_basis_code, "privacy.legal_basis_code", required=False),
        )
        consent_state = _token(self.consent_state, "privacy.consent_state")
        if consent_state not in CONSENT_STATES:
            raise AttributionDTOValidationError("privacy.consent_state is invalid")
        object.__setattr__(self, "consent_state", consent_state)
        object.__setattr__(
            self,
            "decision_source",
            _token(self.decision_source, "privacy.decision_source", required=False),
        )
        if self.decided_at is not None:
            object.__setattr__(
                self,
                "decided_at",
                _datetime(self.decided_at, "privacy.decided_at"),
            )

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> "PrivacySnapshotDTO":
        try:
            payload = dict(values or {})
            if isinstance(payload.get("decided_at"), str):
                payload["decided_at"] = _datetime_from_iso(payload["decided_at"])
            return cls(**payload)
        except AttributionDTOValidationError:
            raise
        except (TypeError, ValueError) as error:
            raise AttributionDTOValidationError("invalid privacy snapshot") from error

    def to_dict(self) -> Dict[str, Any]:
        return _serialize(self)


@dataclasses.dataclass(frozen=True)
class MarketingIdentifierDTO:
    namespace: str
    role: str
    comparison_hash: str
    masked_value: str = ""
    value_ref: str = ""
    source_field: str = ""
    purpose: str = "attribution"
    retain_until: Optional[datetime.date] = None

    def __post_init__(self):
        object.__setattr__(
            self, "namespace", _token(self.namespace, "identifier.namespace")
        )
        object.__setattr__(self, "role", _token(self.role, "identifier.role"))
        comparison_hash = _bounded_text(
            self.comparison_hash,
            "identifier.comparison_hash",
            64,
            required=True,
        ).lower()
        if not _SHA256_RE.fullmatch(comparison_hash):
            raise AttributionDTOValidationError(
                "identifier.comparison_hash must be a SHA-256 digest"
            )
        object.__setattr__(self, "comparison_hash", comparison_hash)
        object.__setattr__(
            self,
            "masked_value",
            _bounded_text(self.masked_value, "identifier.masked_value", 256),
        )
        object.__setattr__(
            self,
            "value_ref",
            _bounded_text(self.value_ref, "identifier.value_ref", 512),
        )
        object.__setattr__(
            self,
            "source_field",
            _bounded_text(self.source_field, "identifier.source_field", 128),
        )
        object.__setattr__(self, "purpose", _token(self.purpose, "identifier.purpose"))
        if self.retain_until is not None and not isinstance(
            self.retain_until, datetime.date
        ):
            raise AttributionDTOValidationError(
                "identifier.retain_until must be a date"
            )

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> "MarketingIdentifierDTO":
        try:
            payload = dict(values)
            retain_until = payload.get("retain_until")
            if isinstance(retain_until, str):
                payload["retain_until"] = datetime.date.fromisoformat(retain_until)
            return cls(**payload)
        except AttributionDTOValidationError:
            raise
        except (TypeError, ValueError) as error:
            raise AttributionDTOValidationError(
                "invalid marketing identifier"
            ) from error

    def to_dict(self) -> Dict[str, Any]:
        return _serialize(self)

    def canonical_dict(self) -> Dict[str, Any]:
        return {
            "namespace": self.namespace,
            "role": self.role,
            "comparison_hash": self.comparison_hash,
            "purpose": self.purpose,
        }


@dataclasses.dataclass(frozen=True)
class MarketingTouchpointDTO:
    source_system: str
    source_scope_ref: str
    source_occurrence_ref: str
    occurred_at: datetime.datetime
    platform: str
    channel: str
    touchpoint_type: str
    evidence_level: str
    revision_kind: str = "observation"
    observed_at: Optional[datetime.datetime] = None
    source_evidence_ref: str = ""
    source_schema_version: str = ""
    network: str = ""
    landing_url: str = ""
    referrer_url: str = ""
    utm: Dict[str, Any] = dataclasses.field(default_factory=dict)
    asset_refs: Dict[str, Any] = dataclasses.field(default_factory=dict)
    identifiers: Tuple[MarketingIdentifierDTO, ...] = ()
    privacy: PrivacySnapshotDTO = dataclasses.field(default_factory=PrivacySnapshotDTO)
    extensions: Dict[str, Any] = dataclasses.field(default_factory=dict)
    mapping_version: int = 1
    schema_version: int = MARKETING_TOUCHPOINT_SCHEMA_VERSION

    def __post_init__(self):
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != MARKETING_TOUCHPOINT_SCHEMA_VERSION
        ):
            raise AttributionDTOValidationError(
                "unsupported MarketingTouchpointDTO schema_version"
            )
        if (
            not isinstance(self.mapping_version, int)
            or isinstance(self.mapping_version, bool)
            or self.mapping_version < 1
        ):
            raise AttributionDTOValidationError(
                "mapping_version must be a positive integer"
            )
        object.__setattr__(
            self, "source_system", _token(self.source_system, "source_system")
        )
        object.__setattr__(
            self,
            "source_scope_ref",
            _bounded_text(
                self.source_scope_ref, "source_scope_ref", 512, required=True
            ),
        )
        object.__setattr__(
            self,
            "source_occurrence_ref",
            _bounded_text(
                self.source_occurrence_ref,
                "source_occurrence_ref",
                512,
                required=True,
            ),
        )
        object.__setattr__(
            self,
            "source_evidence_ref",
            _bounded_text(self.source_evidence_ref, "source_evidence_ref", 512),
        )
        object.__setattr__(
            self,
            "source_schema_version",
            _bounded_text(self.source_schema_version, "source_schema_version", 64),
        )
        object.__setattr__(
            self, "occurred_at", _datetime(self.occurred_at, "occurred_at")
        )
        object.__setattr__(
            self,
            "observed_at",
            _datetime(self.observed_at or self.occurred_at, "observed_at"),
        )
        object.__setattr__(self, "platform", _token(self.platform, "platform"))
        object.__setattr__(self, "channel", _token(self.channel, "channel"))
        object.__setattr__(
            self,
            "network",
            _token(self.network, "network", required=False),
        )
        touchpoint_type = _token(self.touchpoint_type, "touchpoint_type")
        if touchpoint_type not in TOUCHPOINT_TYPES:
            raise AttributionDTOValidationError("touchpoint_type is invalid")
        object.__setattr__(self, "touchpoint_type", touchpoint_type)
        evidence_level = _token(self.evidence_level, "evidence_level")
        if evidence_level not in EVIDENCE_LEVELS:
            raise AttributionDTOValidationError("evidence_level is invalid")
        object.__setattr__(self, "evidence_level", evidence_level)
        revision_kind = _token(self.revision_kind, "revision_kind")
        if revision_kind not in REVISION_KINDS:
            raise AttributionDTOValidationError("revision_kind is invalid")
        object.__setattr__(self, "revision_kind", revision_kind)
        object.__setattr__(
            self, "landing_url", sanitize_url(self.landing_url, "landing_url")
        )
        object.__setattr__(
            self,
            "referrer_url",
            sanitize_url(self.referrer_url, "referrer_url"),
        )
        utm = _json_mapping(self.utm, "utm", allowed_keys=_UTM_KEYS)
        for key, value in utm.items():
            utm[key] = _bounded_text(value, "utm.%s" % key, 512)
        object.__setattr__(self, "utm", utm)
        asset_refs = _json_mapping(self.asset_refs, "asset_refs", namespaced_keys=True)
        for key, value in asset_refs.items():
            asset_refs[key] = _bounded_text(value, "asset_refs.%s" % key, 512)
        object.__setattr__(self, "asset_refs", asset_refs)
        identifiers = tuple(
            (
                item
                if isinstance(item, MarketingIdentifierDTO)
                else MarketingIdentifierDTO.from_dict(item)
            )
            for item in tuple(self.identifiers or ())
        )
        if len(identifiers) > 64:
            raise AttributionDTOValidationError(
                "identifiers exceeds the supported limit"
            )
        identifier_keys = {
            (item.namespace, item.role, item.comparison_hash) for item in identifiers
        }
        if len(identifier_keys) != len(identifiers):
            raise AttributionDTOValidationError("identifiers contains duplicates")
        object.__setattr__(self, "identifiers", identifiers)
        privacy = self.privacy
        if not isinstance(privacy, PrivacySnapshotDTO):
            privacy = PrivacySnapshotDTO.from_dict(privacy or {})
        object.__setattr__(self, "privacy", privacy)
        object.__setattr__(
            self,
            "extensions",
            _json_mapping(self.extensions, "extensions", namespaced_keys=True),
        )

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> "MarketingTouchpointDTO":
        try:
            payload = dict(values)
            for field_name in ("occurred_at", "observed_at"):
                if isinstance(payload.get(field_name), str):
                    payload[field_name] = _datetime_from_iso(payload[field_name])
            payload["identifiers"] = tuple(
                (
                    item
                    if isinstance(item, MarketingIdentifierDTO)
                    else MarketingIdentifierDTO.from_dict(item)
                )
                for item in tuple(payload.get("identifiers") or ())
            )
            if not isinstance(payload.get("privacy"), PrivacySnapshotDTO):
                payload["privacy"] = PrivacySnapshotDTO.from_dict(
                    payload.get("privacy") or {}
                )
            return cls(**payload)
        except AttributionDTOValidationError:
            raise
        except (TypeError, ValueError) as error:
            raise AttributionDTOValidationError(
                "invalid marketing touchpoint payload"
            ) from error

    def to_dict(self) -> Dict[str, Any]:
        return _serialize(self)

    def canonical_identity(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "source_system": self.source_system,
            "source_scope_ref": self.source_scope_ref,
            "source_occurrence_ref": self.source_occurrence_ref,
            "touchpoint_type": self.touchpoint_type,
        }

    def canonical_content(self) -> Dict[str, Any]:
        return {
            "version": self.schema_version,
            "source_system": self.source_system,
            "source_scope_ref": self.source_scope_ref,
            "source_occurrence_ref": self.source_occurrence_ref,
            "occurred_at": self.occurred_at.isoformat(),
            "platform": self.platform,
            "channel": self.channel,
            "touchpoint_type": self.touchpoint_type,
            "evidence_level": self.evidence_level,
            "revision_kind": self.revision_kind,
            "network": self.network,
            "landing_url": self.landing_url,
            "referrer_url": self.referrer_url,
            "utm": self.utm,
            "asset_refs": self.asset_refs,
            "identifiers": sorted(
                (item.canonical_dict() for item in self.identifiers),
                key=lambda item: (
                    item["namespace"],
                    item["role"],
                    item["comparison_hash"],
                ),
            ),
            "privacy": self.privacy.to_dict(),
            "extensions": self.extensions,
        }

    @property
    def canonical_key(self) -> str:
        return sha256_text(canonical_json(self.canonical_identity()))

    @property
    def content_hash(self) -> str:
        return sha256_text(canonical_json(self.canonical_content()))


@dataclasses.dataclass(frozen=True)
class AttributionIngestResult:
    touchpoint_id: int
    public_ref: str
    disposition: str
    revision_sequence: int
    canonical_key: str
    content_hash: str

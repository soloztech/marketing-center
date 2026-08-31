import dataclasses
import datetime
import json
import math
import re
from typing import Any, Dict, Mapping, Optional, Tuple

from .dto import canonical_json, sha256_text

EXTERNAL_ENTITY_SCHEMA_VERSION = 1
SYNC_PAGE_SCHEMA_VERSION = 1

_MAX_JSON_BYTES = 32 * 1024
_MAX_JSON_DEPTH = 8
_MAX_JSON_NODES = 1024
_MAX_PAGE_ITEMS = 2000
_MAX_PAGE_ERRORS = 200
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_SECRET_KEY_RE = re.compile(r"[^a-z0-9]")
_SECRET_KEYS = frozenset(
    {
        "accesstoken",
        "apikey",
        "apisecret",
        "appsecret",
        "authorization",
        "clientsecret",
        "credential",
        "credentials",
        "password",
        "passwd",
        "privatekey",
        "refreshtoken",
        "secret",
        "secretkey",
        "sessioncookie",
        "token",
        "webhooksecret",
    }
)
EMPTY_REPORTING_CONTEXT_HASH = sha256_text(canonical_json({}))


class CatalogDTOValidationError(ValueError):
    """Raised when a provider-neutral catalog contract is invalid."""


def _bounded_text(
    value: Any,
    field_name: str,
    limit: int,
    *,
    required: bool = False,
) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise CatalogDTOValidationError("%s must be text" % field_name)
    normalized = value.strip()
    if required and not normalized:
        raise CatalogDTOValidationError("%s is required" % field_name)
    if len(normalized) > limit:
        raise CatalogDTOValidationError("%s is too long" % field_name)
    if any(ord(character) < 32 for character in normalized):
        raise CatalogDTOValidationError("%s contains control characters" % field_name)
    return normalized


def _token(value: Any, field_name: str, *, required: bool = True) -> str:
    normalized = _bounded_text(
        value,
        field_name,
        128,
        required=required,
    ).lower()
    if normalized and not _TOKEN_RE.fullmatch(normalized):
        raise CatalogDTOValidationError("%s is invalid" % field_name)
    return normalized


def _schema_version(value: Any, expected: int, contract_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value != expected:
        raise CatalogDTOValidationError("unsupported %s schema_version" % contract_name)
    return value


def _datetime(value: Any, field_name: str) -> datetime.datetime:
    if not isinstance(value, datetime.datetime):
        raise CatalogDTOValidationError("%s must be a datetime" % field_name)
    if value.tzinfo:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)


def _optional_datetime(
    value: Any,
    field_name: str,
) -> Optional[datetime.datetime]:
    if value is None:
        return None
    return _datetime(value, field_name)


def _datetime_from_iso(value: Any, field_name: str) -> datetime.datetime:
    if not isinstance(value, str):
        raise CatalogDTOValidationError("%s must use ISO-8601" % field_name)
    try:
        normalized = "%s+00:00" % value[:-1] if value.endswith("Z") else value
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError as error:
        raise CatalogDTOValidationError("%s must use ISO-8601" % field_name) from error
    return _datetime(parsed, field_name)


def _wire_datetime(value: datetime.datetime) -> str:
    return "%sZ" % value.isoformat()


def _is_secret_key(key: str) -> bool:
    normalized = _SECRET_KEY_RE.sub("", key.lower())
    return any(
        normalized == denied or normalized.endswith(denied) for denied in _SECRET_KEYS
    )


def _normalize_json(
    value: Any,
    field_name: str,
    *,
    depth: int = 0,
    counter: Optional[list] = None,
) -> Any:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > _MAX_JSON_NODES:
        raise CatalogDTOValidationError("%s contains too many values" % field_name)
    if depth > _MAX_JSON_DEPTH:
        raise CatalogDTOValidationError("%s is nested too deeply" % field_name)

    if value is None or isinstance(value, (bool, str)):
        if isinstance(value, str):
            return _bounded_text(value, field_name, 4096)
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if value < -(2**63) or value > 2**63 - 1:
            raise CatalogDTOValidationError(
                "%s contains an oversized integer" % field_name
            )
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CatalogDTOValidationError(
                "%s contains a non-finite number" % field_name
            )
        return value
    if isinstance(value, Mapping):
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CatalogDTOValidationError("%s keys must be text" % field_name)
            clean_key = _bounded_text(key, "%s key" % field_name, 128, required=True)
            if _is_secret_key(clean_key):
                raise CatalogDTOValidationError(
                    "%s contains a forbidden secret key" % field_name
                )
            if clean_key in normalized:
                raise CatalogDTOValidationError(
                    "%s contains duplicate normalized keys" % field_name
                )
            normalized[clean_key] = _normalize_json(
                item,
                "%s.%s" % (field_name, clean_key),
                depth=depth + 1,
                counter=counter,
            )
        return normalized
    if isinstance(value, (list, tuple)):
        return [
            _normalize_json(
                item,
                "%s[]" % field_name,
                depth=depth + 1,
                counter=counter,
            )
            for item in value
        ]
    raise CatalogDTOValidationError("%s must contain only JSON values" % field_name)


def _json_mapping(value: Any, field_name: str) -> Dict[str, Any]:
    if value in (None, False):
        return {}
    if not isinstance(value, Mapping):
        raise CatalogDTOValidationError("%s must be an object" % field_name)
    normalized = _normalize_json(value, field_name)
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise CatalogDTOValidationError(
            "%s must be JSON serializable" % field_name
        ) from error
    if len(encoded) > _MAX_JSON_BYTES:
        raise CatalogDTOValidationError("%s is too large" % field_name)
    return normalized


def _serialize_json(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: _serialize_json(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, datetime.datetime):
        return _wire_datetime(value)
    if isinstance(value, tuple):
        return [_serialize_json(item) for item in value]
    if isinstance(value, list):
        return [_serialize_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize_json(item) for key, item in value.items()}
    return value


@dataclasses.dataclass(frozen=True)
class ExternalEntityDTO:
    entity_type: str
    external_ref: str
    observed_at: datetime.datetime
    external_id: str = ""
    name: str = ""
    remote_status: str = ""
    group_type: str = ""
    parent_entity_type: str = ""
    parent_external_ref: str = ""
    provider_updated_at: Optional[datetime.datetime] = None
    remote_missing_at: Optional[datetime.datetime] = None
    attributes: Dict[str, Any] = dataclasses.field(default_factory=dict)
    source_schema_version: str = ""
    schema_version: int = EXTERNAL_ENTITY_SCHEMA_VERSION

    def __post_init__(self):
        _schema_version(
            self.schema_version,
            EXTERNAL_ENTITY_SCHEMA_VERSION,
            "ExternalEntityDTO",
        )
        object.__setattr__(self, "entity_type", _token(self.entity_type, "entity_type"))
        # Provider identifiers are opaque and intentionally case-sensitive.
        object.__setattr__(
            self,
            "external_ref",
            _bounded_text(self.external_ref, "external_ref", 1024, required=True),
        )
        object.__setattr__(
            self,
            "external_id",
            _bounded_text(self.external_id, "external_id", 512),
        )
        object.__setattr__(self, "name", _bounded_text(self.name, "name", 1024))
        object.__setattr__(
            self,
            "remote_status",
            _token(self.remote_status, "remote_status", required=False),
        )
        object.__setattr__(
            self,
            "group_type",
            _token(self.group_type, "group_type", required=False),
        )
        object.__setattr__(
            self,
            "parent_entity_type",
            _token(
                self.parent_entity_type,
                "parent_entity_type",
                required=False,
            ),
        )
        object.__setattr__(
            self,
            "parent_external_ref",
            _bounded_text(self.parent_external_ref, "parent_external_ref", 1024),
        )
        if bool(self.parent_entity_type) != bool(self.parent_external_ref):
            raise CatalogDTOValidationError(
                "parent_entity_type and parent_external_ref must be supplied together"
            )
        object.__setattr__(
            self,
            "observed_at",
            _datetime(self.observed_at, "observed_at"),
        )
        for field_name in (
            "provider_updated_at",
            "remote_missing_at",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_datetime(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "attributes",
            _json_mapping(self.attributes, "attributes"),
        )
        object.__setattr__(
            self,
            "source_schema_version",
            _bounded_text(
                self.source_schema_version,
                "source_schema_version",
                128,
            ),
        )

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> "ExternalEntityDTO":
        if not isinstance(values, Mapping):
            raise CatalogDTOValidationError("external entity payload must be an object")
        try:
            payload = dict(values)
            for field_name in (
                "observed_at",
                "provider_updated_at",
                "remote_missing_at",
            ):
                if isinstance(payload.get(field_name), str):
                    payload[field_name] = _datetime_from_iso(
                        payload[field_name],
                        field_name,
                    )
            return cls(**payload)
        except CatalogDTOValidationError:
            raise
        except (TypeError, ValueError) as error:
            raise CatalogDTOValidationError(
                "invalid external entity payload"
            ) from error

    def to_dict(self) -> Dict[str, Any]:
        return _serialize_json(self)

    def canonical_identity(self) -> Dict[str, Any]:
        return {
            "version": self.schema_version,
            "entity_type": self.entity_type,
            "external_ref": self.external_ref,
        }

    def canonical_content(self) -> Dict[str, Any]:
        # observed_at records transport/observation time, not provider state.
        return {
            "version": self.schema_version,
            "entity_type": self.entity_type,
            "external_ref": self.external_ref,
            "external_id": self.external_id,
            "name": self.name,
            "remote_status": self.remote_status,
            "group_type": self.group_type,
            "parent_entity_type": self.parent_entity_type,
            "parent_external_ref": self.parent_external_ref,
            "remote_missing_at": _serialize_json(self.remote_missing_at),
            "attributes": self.attributes,
            "source_schema_version": self.source_schema_version,
        }

    @property
    def canonical_key(self) -> str:
        return sha256_text(canonical_json(self.canonical_identity()))

    @property
    def content_hash(self) -> str:
        return sha256_text(canonical_json(self.canonical_content()))


@dataclasses.dataclass(frozen=True)
class SyncPageDTO:
    items: Tuple[ExternalEntityDTO, ...] = ()
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
    schema_version: int = SYNC_PAGE_SCHEMA_VERSION

    def __post_init__(self):
        _schema_version(self.schema_version, SYNC_PAGE_SCHEMA_VERSION, "SyncPageDTO")
        items = tuple(
            item
            if isinstance(item, ExternalEntityDTO)
            else ExternalEntityDTO.from_dict(item)
            for item in tuple(self.items or ())
        )
        if len(items) > _MAX_PAGE_ITEMS:
            raise CatalogDTOValidationError("items exceeds the supported page limit")
        identities = {(item.entity_type, item.external_ref) for item in items}
        if len(identities) != len(items):
            raise CatalogDTOValidationError(
                "items contains duplicate entity identities"
            )
        object.__setattr__(self, "items", items)

        if not isinstance(self.has_more, bool):
            raise CatalogDTOValidationError("has_more must be a boolean")
        object.__setattr__(
            self,
            "next_cursor",
            _bounded_text(self.next_cursor, "next_cursor", 4096),
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
            _token(
                self.provider_job_state,
                "provider_job_state",
                required=False,
            ),
        )
        if bool(self.provider_job_ref) != bool(self.provider_job_state):
            raise CatalogDTOValidationError(
                "provider_job_ref and provider_job_state must be supplied together"
            )
        if self.has_more and not (self.next_cursor or self.provider_job_ref):
            raise CatalogDTOValidationError(
                "has_more requires next_cursor or provider_job_ref"
            )
        object.__setattr__(
            self,
            "watermark",
            _bounded_text(self.watermark, "watermark", 4096),
        )
        context_hash = _bounded_text(
            self.reporting_context_hash,
            "reporting_context_hash",
            64,
        ).lower()
        if not _SHA256_RE.fullmatch(context_hash):
            raise CatalogDTOValidationError(
                "reporting_context_hash must be a SHA-256 digest"
            )
        object.__setattr__(self, "reporting_context_hash", context_hash)
        if (
            not isinstance(self.retry_after, int)
            or isinstance(self.retry_after, bool)
            or self.retry_after < 0
            or self.retry_after > 86400
        ):
            raise CatalogDTOValidationError(
                "retry_after must be between 0 and 86400 seconds"
            )
        errors = tuple(
            _json_mapping(error, "errors") for error in tuple(self.errors or ())
        )
        if len(errors) > _MAX_PAGE_ERRORS:
            raise CatalogDTOValidationError("errors exceeds the supported page limit")
        object.__setattr__(self, "errors", errors)
        if not isinstance(self.authoritative_complete, bool):
            raise CatalogDTOValidationError("authoritative_complete must be a boolean")
        if self.authoritative_complete and self.has_more:
            raise CatalogDTOValidationError(
                "an authoritative complete page cannot have more pages"
            )

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> "SyncPageDTO":
        if not isinstance(values, Mapping):
            raise CatalogDTOValidationError("sync page payload must be an object")
        try:
            payload = dict(values)
            payload["items"] = tuple(
                item
                if isinstance(item, ExternalEntityDTO)
                else ExternalEntityDTO.from_dict(item)
                for item in tuple(payload.get("items") or ())
            )
            payload["errors"] = tuple(payload.get("errors") or ())
            return cls(**payload)
        except CatalogDTOValidationError:
            raise
        except (TypeError, ValueError) as error:
            raise CatalogDTOValidationError("invalid sync page payload") from error

    def to_dict(self) -> Dict[str, Any]:
        return _serialize_json(self)


@dataclasses.dataclass(frozen=True)
class EntityIngestResult:
    entity_id: int
    public_ref: str
    disposition: str
    revision_sequence: int
    content_hash: str

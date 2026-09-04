import dataclasses
import datetime
import hashlib
import ipaddress
import json
import re
from typing import Dict, Mapping, Tuple
from urllib.parse import unquote, urlsplit, urlunsplit

MAX_BODY_BYTES = 16 * 1024
MAX_PAYLOAD_FIELDS = 20
CLICK_ID_FIELDS = ("gclid", "gbraid", "wbraid", "fbclid")
UTM_FIELDS = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
)
REFERENCE_FIELDS = ("session_ref", "visitor_ref")
ACTION_REFERENCE_FIELDS = ("action_ref", "route_ref", "model_ref")
ALLOWED_PAYLOAD_FIELDS = frozenset(
    {
        "event_id",
        "event_type",
        "occurred_at",
        "landing_url",
        "referrer_url",
        "consent_state",
        *CLICK_ID_FIELDS,
        *UTM_FIELDS,
        *REFERENCE_FIELDS,
        *ACTION_REFERENCE_FIELDS,
    }
)
ACTION_EVENT_TYPES = frozenset({"form_submission", "organic_link"})
EVENT_TYPES = frozenset({"entry_point", *ACTION_EVENT_TYPES})
CONSENT_STATES = frozenset({"denied", "granted", "unknown"})
INGRESS_PROVENANCE = frozenset(
    {"browser_capability", "server_internal", "website_confirmed_action"}
)
PUBLIC_INGRESS_EVENT_TYPES = frozenset({"entry_point"})
_OPAQUE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:~-]{0,511}$")
_EVENT_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class WebIngressContractError(ValueError):
    """Raised for a rejected, provider-neutral first-party envelope."""


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text(value, field_name, limit, *, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise WebIngressContractError("%s must be text" % field_name)
    normalized = value.strip()
    if required and not normalized:
        raise WebIngressContractError("%s is required" % field_name)
    if len(normalized) > limit:
        raise WebIngressContractError("%s is too long" % field_name)
    if _CONTROL_RE.search(normalized):
        raise WebIngressContractError("%s contains control characters" % field_name)
    return normalized


def _lines(value, field_name):
    if not isinstance(value, str):
        raise WebIngressContractError("%s must be text" % field_name)
    if len(value) > 8192:
        raise WebIngressContractError("%s is too long" % field_name)
    if any(character in value for character in ("\x00", "\r", "\t")):
        raise WebIngressContractError("%s contains control characters" % field_name)
    lines = tuple(line.strip() for line in value.split("\n") if line.strip())
    if not lines:
        raise WebIngressContractError("%s is required" % field_name)
    return lines


def _opaque_token(value, field_name, limit=512, *, required=False):
    normalized = _text(value, field_name, limit, required=required)
    if normalized and not _OPAQUE_TOKEN_RE.fullmatch(normalized):
        raise WebIngressContractError("%s is invalid" % field_name)
    return normalized


def _event_token(value):
    normalized = _text(value, "event_id", 128, required=True)
    if len(normalized) < 16 or not _EVENT_TOKEN_RE.fullmatch(normalized):
        raise WebIngressContractError("event_id is invalid")
    return normalized


def _action_references(payload, event_type, max_field_length):
    field_presence = {
        field_name: field_name in payload for field_name in ACTION_REFERENCE_FIELDS
    }
    if event_type == "entry_point" and any(field_presence.values()):
        raise WebIngressContractError("entry_point must not contain action references")
    if event_type == "organic_link" and field_presence["model_ref"]:
        raise WebIngressContractError("organic_link must not contain model_ref")
    return (
        _opaque_token(
            payload.get("action_ref"),
            "action_ref",
            max_field_length,
            required=event_type in ACTION_EVENT_TYPES,
        ),
        _opaque_token(
            payload.get("route_ref"),
            "route_ref",
            max_field_length,
            required=event_type in ACTION_EVENT_TYPES,
        ),
        _opaque_token(
            payload.get("model_ref"),
            "model_ref",
            max_field_length,
            required=event_type == "form_submission",
        ),
    )


def _provider_datetime(value):
    normalized = _text(value, "occurred_at", 40, required=True)
    if normalized.endswith("Z"):
        normalized = "%s+00:00" % normalized[:-1]
    match = re.fullmatch(r"(.+)([+-]\d{2})(\d{2})", normalized)
    if match and ":" not in normalized[-6:]:
        normalized = "%s%s:%s" % match.groups()
    try:
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError as error:
        raise WebIngressContractError("occurred_at is invalid") from error
    if parsed.tzinfo is None:
        raise WebIngressContractError("occurred_at must include a timezone")
    return parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)


def _hostname(value, field_name):
    normalized = _text(value, field_name, 253, required=True).lower().rstrip(".")
    if (
        "*" in normalized
        or "://" in normalized
        or "/" in normalized
        or "@" in normalized
    ):
        raise WebIngressContractError("%s must be an exact hostname" % field_name)
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        address = None
    if address is not None:
        return address.compressed.lower()
    try:
        normalized = normalized.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise WebIngressContractError("%s is invalid" % field_name) from error
    labels = normalized.split(".")
    if (
        not normalized
        or len(normalized) > 253
        or any(not _DNS_LABEL_RE.fullmatch(label) for label in labels)
    ):
        raise WebIngressContractError("%s is invalid" % field_name)
    return normalized


def normalize_origin(value):
    normalized = _text(value, "origin", 512, required=True)
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as error:
        raise WebIngressContractError("origin is invalid") from error
    if (
        parsed.scheme.lower() not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise WebIngressContractError("origin is invalid")
    hostname = _hostname(parsed.hostname, "origin host")
    if ":" in hostname:
        hostname = "[%s]" % hostname
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    port_suffix = "" if port in (None, default_port) else ":%s" % port
    return "%s://%s%s" % (parsed.scheme.lower(), hostname, port_suffix)


def normalize_allowed_origins(value) -> Tuple[str, ...]:
    lines = _lines(value, "allowed_origins")
    origins = tuple(sorted({normalize_origin(line) for line in lines if line.strip()}))
    if not origins or len(origins) > 64:
        raise WebIngressContractError("allowed_origins has an invalid count")
    return origins


def normalize_allowed_hosts(value) -> Tuple[str, ...]:
    lines = _lines(value, "allowed_hosts")
    hosts = tuple(
        sorted({_hostname(line, "allowed host") for line in lines if line.strip()})
    )
    if not hosts or len(hosts) > 64:
        raise WebIngressContractError("allowed_hosts has an invalid count")
    return hosts


def _origin_from_safe_url(value):
    parsed = urlsplit(value)
    return normalize_origin(urlunsplit((parsed.scheme, parsed.netloc, "", "", "")))


def _safe_url(value, field_name):
    normalized = _text(value, field_name, 2048, required=field_name == "landing_url")
    if not normalized:
        return ""
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as error:
        raise WebIngressContractError("%s is invalid" % field_name) from error
    if (
        parsed.scheme.lower() not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise WebIngressContractError("%s is invalid" % field_name)
    hostname = _hostname(parsed.hostname, "%s host" % field_name)
    if ":" in hostname and not hostname.startswith("["):
        hostname = "[%s]" % hostname
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    netloc = hostname if port in (None, default_port) else "%s:%s" % (hostname, port)
    decoded_path = unquote(parsed.path)
    if "\\" in decoded_path or _CONTROL_RE.search(decoded_path):
        raise WebIngressContractError("%s is invalid" % field_name)
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", "", ""))


def _url_host(value, field_name):
    if not value:
        return ""
    try:
        hostname = urlsplit(value).hostname
    except ValueError as error:
        raise WebIngressContractError("%s is invalid" % field_name) from error
    return _hostname(hostname, "%s host" % field_name)


@dataclasses.dataclass(frozen=True)
class WebIngressPayload:
    event_key_hash: str
    event_type: str
    occurred_at: datetime.datetime
    landing_url: str
    landing_host: str
    referrer_url: str
    utm: Dict[str, str]
    click_values: Dict[str, str]
    reference_hashes: Dict[str, str]
    action_ref: str
    route_ref: str
    model_ref: str
    consent_state: str

    def safe_canonical_dict(self):
        canonical = {
            "event_key_hash": self.event_key_hash,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at.isoformat(),
            "landing_url": self.landing_url,
            "landing_host": self.landing_host,
            "referrer_url": self.referrer_url,
            "utm": self.utm,
            "click_hashes": {
                key: _sha256(value) for key, value in self.click_values.items()
            },
            "reference_hashes": self.reference_hashes,
            "consent_state": self.consent_state,
        }
        # Keep the v1 entry-point canonical representation byte-for-byte
        # compatible. Technical action references belong only to v2 action
        # events, so an upgrade cannot turn a valid entry-point replay into a
        # digest conflict.
        if self.event_type in ACTION_EVENT_TYPES:
            canonical.update(
                {
                    "action_ref": self.action_ref,
                    "route_ref": self.route_ref,
                }
            )
            if self.model_ref:
                canonical["model_ref"] = self.model_ref
        return canonical


def parse_web_ingress_payload(
    payload: Mapping,
    *,
    allowed_hosts: Tuple[str, ...],
    expected_origin: str,
    max_field_length: int,
) -> WebIngressPayload:
    if not isinstance(payload, Mapping):
        raise WebIngressContractError("payload must be an object")
    if len(payload) > MAX_PAYLOAD_FIELDS or set(payload) - ALLOWED_PAYLOAD_FIELDS:
        raise WebIngressContractError("payload contains unsupported fields")
    if any(not isinstance(key, str) for key in payload):
        raise WebIngressContractError("payload keys must be text")
    event_id = _event_token(payload.get("event_id"))
    event_type = _text(payload.get("event_type"), "event_type", 32, required=True)
    if event_type not in EVENT_TYPES:
        raise WebIngressContractError("event_type is invalid")
    action_ref, route_ref, model_ref = _action_references(
        payload, event_type, max_field_length
    )
    occurred_at = _provider_datetime(payload.get("occurred_at"))
    landing_url = _safe_url(payload.get("landing_url"), "landing_url")
    landing_host = _url_host(landing_url, "landing_url")
    if landing_host not in allowed_hosts:
        raise WebIngressContractError("landing_url host is not allowed")
    if _origin_from_safe_url(landing_url) != normalize_origin(expected_origin):
        raise WebIngressContractError("landing_url origin does not match origin")
    referrer_url = _safe_url(payload.get("referrer_url"), "referrer_url")
    referrer_host = _url_host(referrer_url, "referrer_url")
    if referrer_url and referrer_host not in allowed_hosts:
        parsed_referrer = urlsplit(referrer_url)
        referrer_url = urlunsplit(
            (parsed_referrer.scheme, parsed_referrer.netloc, "/", "", "")
        )
    utm = {}
    for field_name in UTM_FIELDS:
        value = _text(payload.get(field_name), field_name, max_field_length)
        if value:
            utm[field_name[4:]] = value
    click_values = {}
    for field_name in CLICK_ID_FIELDS:
        value = _opaque_token(
            payload.get(field_name), field_name, max_field_length, required=False
        )
        if value:
            click_values[field_name] = value
    reference_hashes = {}
    for field_name in REFERENCE_FIELDS:
        value = _opaque_token(
            payload.get(field_name), field_name, max_field_length, required=False
        )
        if value:
            if len(value) < 16:
                raise WebIngressContractError("%s is too short" % field_name)
            reference_hashes[field_name] = _sha256(value)
    consent_state = _text(
        payload.get("consent_state") or "unknown", "consent_state", 16
    )
    if consent_state not in CONSENT_STATES:
        raise WebIngressContractError("consent_state is invalid")
    return WebIngressPayload(
        event_key_hash=_sha256(event_id),
        event_type=event_type,
        occurred_at=occurred_at,
        landing_url=landing_url,
        landing_host=landing_host,
        referrer_url=referrer_url,
        utm=utm,
        click_values=click_values,
        reference_hashes=reference_hashes,
        action_ref=action_ref,
        route_ref=route_ref,
        model_ref=model_ref,
        consent_state=consent_state,
    )


def canonical_request_digest(payload: WebIngressPayload, origin: str = "") -> str:
    canonical = payload.safe_canonical_dict()
    if origin:
        canonical["origin"] = normalize_origin(origin)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256(encoded)


@dataclasses.dataclass(frozen=True)
class WebIngressResult:
    disposition: str
    event_ref: str = ""
    touchpoint_id: int = 0

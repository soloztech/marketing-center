import hashlib
import json
import re
import uuid
from urllib.parse import unquote, urlsplit

FORM_QUERY_FIELDS = ("mc_action", "mc_event", "mc_session")
FORM_EXCHANGE_FIELDS = frozenset({"action_ref", "event_id", "session_ref", "receipt"})
WHATSAPP_CLAIM_FIELDS = frozenset({"action_ref", "event_id", "session_ref"})
MAX_ACTION_BODY_BYTES = 2048
ACTION_KINDS = frozenset({"form_submission", "whatsapp_handoff"})
_ROUTE_REF_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_DIGITS_RE = re.compile(r"^[1-9][0-9]{7,14}$")
_RECEIPT_RE = re.compile(r"^(\d{10})\.(\d{10})\.([0-9a-f]{64})$")
_REDIRECT_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class WebsiteActionContractError(ValueError):
    """Raised when a technical Website action envelope is rejected."""


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def opaque_uuid(value, field_name):
    if not isinstance(value, str) or len(value) != 36:
        raise WebsiteActionContractError("%s is invalid" % field_name)
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as error:
        raise WebsiteActionContractError("%s is invalid" % field_name) from error
    if parsed.version != 4 or str(parsed) != value.lower():
        raise WebsiteActionContractError("%s is invalid" % field_name)
    return str(parsed)


def route_ref(value):
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if not _ROUTE_REF_RE.fullmatch(normalized):
        raise WebsiteActionContractError("route_ref is invalid")
    return normalized


def safe_relative_path(value, field_name):
    normalized = value.strip() if isinstance(value, str) else ""
    if (
        not normalized
        or len(normalized) > 512
        or _CONTROL_RE.search(normalized)
        or "\\" in normalized
    ):
        raise WebsiteActionContractError("%s is invalid" % field_name)
    parsed = urlsplit(normalized)
    decoded_path = unquote(parsed.path)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
        or decoded_path.startswith("//")
        or "\\" in decoded_path
        or _CONTROL_RE.search(decoded_path)
    ):
        raise WebsiteActionContractError("%s is invalid" % field_name)
    return parsed.path


def whatsapp_digits(value):
    normalized = value.strip() if isinstance(value, str) else ""
    if not _DIGITS_RE.fullmatch(normalized):
        raise WebsiteActionContractError("WhatsApp destination is invalid")
    return normalized


def strict_object(payload, allowed_fields):
    if not isinstance(payload, dict):
        raise WebsiteActionContractError("payload must be an object")
    if set(payload) != allowed_fields or any(
        not isinstance(key, str) for key in payload
    ):
        raise WebsiteActionContractError("payload fields are invalid")
    return payload


def parse_form_exchange(payload):
    values = strict_object(payload, FORM_EXCHANGE_FIELDS)
    action_ref = opaque_uuid(values.get("action_ref"), "action_ref")
    event_id = opaque_uuid(values.get("event_id"), "event_id")
    session_ref = opaque_uuid(values.get("session_ref"), "session_ref")
    receipt = values.get("receipt")
    match = _RECEIPT_RE.fullmatch(receipt if isinstance(receipt, str) else "")
    if not match:
        raise WebsiteActionContractError("receipt is invalid")
    return {
        "action_ref": action_ref,
        "event_id": event_id,
        "session_ref": session_ref,
        "receipt": receipt,
        "issued_epoch": int(match.group(1)),
        "expires_epoch": int(match.group(2)),
        "signature": match.group(3),
    }


def parse_whatsapp_claim(payload):
    values = strict_object(payload, WHATSAPP_CLAIM_FIELDS)
    return {
        "action_ref": opaque_uuid(values.get("action_ref"), "action_ref"),
        "event_id": opaque_uuid(values.get("event_id"), "event_id"),
        "session_ref": opaque_uuid(values.get("session_ref"), "session_ref"),
    }


def parse_redirect_token(value):
    if not isinstance(value, str) or not _REDIRECT_TOKEN_RE.fullmatch(value):
        raise WebsiteActionContractError("redirect token is invalid")
    return value


def canonical_body_size(payload):
    return len(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )

import dataclasses
import hashlib
import json
import math
import re

META_WEBHOOK_SCHEMA_VERSION = "meta.webhook.v1"
MAX_WEBHOOK_ENTRIES = 100
MAX_CHANGES_PER_ENTRY = 100
MAX_MESSAGING_PER_ENTRY = 100
MAX_WEBHOOK_ITEMS = 1_000

_ID_RE = re.compile(r"^[0-9]{1,40}$")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FORBIDDEN_KEY_PARTS = {
    "access_token",
    "app_secret",
    "authorization",
    "client_secret",
    "cookie",
    "password",
    "signed_url",
    "token",
    "url",
    "verify_token",
}
_MAX_SAFE_DEPTH = 12
_MAX_SAFE_NODES = 2_000
_MAX_SAFE_STRING_BYTES = 64 * 1024
_MAX_RAW_NODES = 100_000


class MetaWebhookSanitizationError(ValueError):
    """A bounded, safely reportable webhook contract failure."""


@dataclasses.dataclass(frozen=True)
class SanitizedWebhook:
    object_type: str
    envelope: dict
    items: tuple
    expected_messaging_keys: tuple


def canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _validated_payload_key(key):
    if not isinstance(key, str) or not key:
        raise MetaWebhookSanitizationError("sanitized payload key is invalid")
    try:
        encoded = key.encode("utf-8")
    except UnicodeEncodeError:
        raise MetaWebhookSanitizationError("sanitized payload key is invalid") from None
    if len(encoded) > 512 or any(
        ord(character) < 32 or ord(character) == 127 for character in key
    ):
        raise MetaWebhookSanitizationError("sanitized payload key is invalid")
    normalized = key.strip().lower()
    if any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
        raise MetaWebhookSanitizationError("sanitized payload contains a protected key")


def _validate_payload_text(value):
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise MetaWebhookSanitizationError(
            "sanitized payload contains invalid text"
        ) from None
    if len(encoded) > _MAX_SAFE_STRING_BYTES:
        raise MetaWebhookSanitizationError("sanitized payload contains oversized text")


def _validate_sanitized_node(item, depth, remaining):
    remaining[0] -= 1
    if remaining[0] < 0 or depth > _MAX_SAFE_DEPTH:
        raise MetaWebhookSanitizationError("sanitized payload is too complex")
    if isinstance(item, dict):
        for key, child in item.items():
            _validated_payload_key(key)
            _validate_sanitized_node(child, depth + 1, remaining)
        return
    if isinstance(item, (list, tuple)):
        for child in item:
            _validate_sanitized_node(child, depth + 1, remaining)
        return
    if isinstance(item, str):
        _validate_payload_text(item)
        return
    if isinstance(item, float) and not math.isfinite(item):
        raise MetaWebhookSanitizationError(
            "sanitized payload contains a non-finite number"
        )
    if item is not None and not isinstance(item, (int, float, bool)):
        raise MetaWebhookSanitizationError(
            "sanitized payload contains an invalid value"
        )


def validate_sanitized_payload(value):
    """Reject unbounded or credential-bearing consumer projections."""

    if not isinstance(value, dict):
        raise MetaWebhookSanitizationError("sanitized payload must be an object")
    _validate_sanitized_node(value, 0, [_MAX_SAFE_NODES])
    return value


def _validate_raw_numbers(value):
    """Reject values which are not valid JSON numbers before allow-listing.

    Python's standard JSON decoder accepts ``NaN`` and infinities by default,
    even though RFC 8259 does not.  This preflight is iterative, cycle-safe and
    bounded so malformed provider input cannot hide a non-finite number inside
    a branch that a particular consumer happens to discard.
    """

    pending = [value]
    visited = set()
    remaining = _MAX_RAW_NODES
    while pending:
        item = pending.pop()
        remaining -= 1
        if remaining < 0:
            raise MetaWebhookSanitizationError("webhook envelope is too complex")
        if isinstance(item, float) and not math.isfinite(item):
            raise MetaWebhookSanitizationError(
                "webhook envelope contains a non-finite number"
            )
        if not isinstance(item, (dict, list, tuple)):
            continue
        marker = id(item)
        if marker in visited:
            continue
        visited.add(marker)
        pending.extend(item.values() if isinstance(item, dict) else item)


def _text(value, field_name, maximum, *, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise MetaWebhookSanitizationError("%s must be text" % field_name)
    value = value.strip()
    if required and not value:
        raise MetaWebhookSanitizationError("%s is required" % field_name)
    if len(value) > maximum or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise MetaWebhookSanitizationError("%s is invalid" % field_name)
    return value


def _token(value, field_name):
    value = _text(value, field_name, 64, required=True).lower()
    if not _FIELD_RE.fullmatch(value):
        raise MetaWebhookSanitizationError("%s is invalid" % field_name)
    return value


def _identifier(value, field_name, *, required=False):
    value = _text(value, field_name, 40, required=required)
    if value and not _ID_RE.fullmatch(value):
        raise MetaWebhookSanitizationError("%s is invalid" % field_name)
    return value


def _timestamp(value, field_name, *, required=False):
    if value is None and not required:
        return None
    if isinstance(value, bool):
        raise MetaWebhookSanitizationError("%s is invalid" % field_name)
    try:
        value = int(value)
    except (OverflowError, TypeError, ValueError):
        raise MetaWebhookSanitizationError("%s is invalid" % field_name) from None
    if value < 0 or value > 9_999_999_999_999:
        raise MetaWebhookSanitizationError("%s is invalid" % field_name)
    return value


def _leadgen_value(value, entry_id, field_name):
    if not isinstance(value, dict):
        raise MetaWebhookSanitizationError("%s must be an object" % field_name)
    result = {
        "leadgen_id": _identifier(
            value.get("leadgen_id"), "%s.leadgen_id" % field_name, required=True
        ),
        "page_id": _identifier(
            value.get("page_id"), "%s.page_id" % field_name, required=True
        ),
        "form_id": _identifier(
            value.get("form_id"), "%s.form_id" % field_name, required=True
        ),
        "created_time": _timestamp(
            value.get("created_time"),
            "%s.created_time" % field_name,
            required=True,
        ),
    }
    for key in ("ad_id", "adgroup_id"):
        identifier = _identifier(value.get(key), "%s.%s" % (field_name, key))
        if identifier:
            result[key] = identifier
    if result["page_id"] != entry_id:
        raise MetaWebhookSanitizationError(
            "%s.page_id does not match the entry" % field_name
        )
    return result


def _item_spec(
    *,
    sequence,
    item_key,
    kind,
    object_type,
    event_field,
    target_asset_id,
    occurrence_ref,
    payload,
):
    return {
        "sequence": sequence,
        "item_key": item_key,
        "kind": kind,
        "object_type": object_type,
        "event_field": event_field,
        "target_asset_id": target_asset_id,
        "occurrence_ref": occurrence_ref,
        "payload_json": payload,
        "event_sha256": canonical_digest(payload),
    }


def _change(entry, entry_index, change, change_index, object_type):
    field_name = "entry[%s].changes[%s]" % (entry_index, change_index)
    item_key = "entry:%s:change:%s" % (entry_index, change_index)
    sequence = entry_index * 1000 + change_index
    if not isinstance(change, dict):
        payload = {
            "entry": entry,
            "reason": "invalid_change",
        }
        return payload, _item_spec(
            sequence=sequence,
            item_key=item_key,
            kind="unknown",
            object_type=object_type,
            event_field="unknown",
            target_asset_id=entry["id"],
            occurrence_ref=item_key,
            payload=payload,
        )
    try:
        event_field = _token(change.get("field"), "%s.field" % field_name)
    except MetaWebhookSanitizationError:
        event_field = "unknown"
    if event_field != "leadgen":
        payload = {
            "entry": entry,
            "field": event_field,
            "reason": "unsupported_change",
        }
        return payload, _item_spec(
            sequence=sequence,
            item_key=item_key,
            kind="unknown",
            object_type=object_type,
            event_field=event_field,
            target_asset_id=entry["id"],
            occurrence_ref=item_key,
            payload=payload,
        )
    try:
        value = _leadgen_value(change.get("value"), entry["id"], field_name)
    except MetaWebhookSanitizationError:
        payload = {
            "entry": entry,
            "field": "leadgen",
            "reason": "invalid_leadgen",
        }
        return payload, _item_spec(
            sequence=sequence,
            item_key=item_key,
            kind="unknown",
            object_type=object_type,
            event_field="leadgen",
            target_asset_id=entry["id"],
            occurrence_ref=item_key,
            payload=payload,
        )
    payload = {
        "schema_version": META_WEBHOOK_SCHEMA_VERSION,
        "object": object_type,
        "entry": entry,
        "field": "leadgen",
        "value": value,
    }
    return payload, _item_spec(
        sequence=sequence,
        item_key=item_key,
        kind="leadgen",
        object_type=object_type,
        event_field="leadgen",
        target_asset_id=value["page_id"],
        occurrence_ref="leadgen:%s:%s" % (value["page_id"], value["leadgen_id"]),
        payload=payload,
    )


def _entry(value, entry_index, object_type):
    if not isinstance(value, dict):
        raise MetaWebhookSanitizationError("entry[%s] must be an object" % entry_index)
    entry = {
        "id": _identifier(value.get("id"), "entry[%s].id" % entry_index, required=True)
    }
    entry_time = _timestamp(value.get("time"), "entry[%s].time" % entry_index)
    if entry_time is not None:
        entry["time"] = entry_time
    changes = value.get("changes", [])
    messaging = value.get("messaging", [])
    if not isinstance(changes, list) or len(changes) > MAX_CHANGES_PER_ENTRY:
        raise MetaWebhookSanitizationError("entry[%s].changes is invalid" % entry_index)
    if not isinstance(messaging, list) or len(messaging) > MAX_MESSAGING_PER_ENTRY:
        raise MetaWebhookSanitizationError(
            "entry[%s].messaging is invalid" % entry_index
        )
    sanitized_changes = []
    items = []
    for change_index, change in enumerate(changes):
        sanitized, item = _change(
            entry,
            entry_index,
            change,
            change_index,
            object_type,
        )
        sanitized_changes.append(sanitized)
        items.append(item)
    sanitized_entry = dict(entry)
    sanitized_entry["changes"] = sanitized_changes
    sanitized_entry["messaging_count"] = len(messaging)
    expected_messaging_keys = tuple(
        "entry:%s:messaging:%s" % (entry_index, item_index)
        for item_index in range(len(messaging))
    )
    return sanitized_entry, tuple(items), expected_messaging_keys


def sanitized_webhook(value):
    if not isinstance(value, dict):
        raise MetaWebhookSanitizationError("webhook envelope must be an object")
    _validate_raw_numbers(value)
    object_type = _token(value.get("object"), "object")
    entries = value.get("entry")
    if not isinstance(entries, list) or len(entries) > MAX_WEBHOOK_ENTRIES:
        raise MetaWebhookSanitizationError("entry must be a bounded array")
    sanitized_entries = []
    items = []
    messaging_keys = []
    for entry_index, entry_value in enumerate(entries):
        entry, entry_items, entry_messaging_keys = _entry(
            entry_value,
            entry_index,
            object_type,
        )
        sanitized_entries.append(entry)
        items.extend(entry_items)
        messaging_keys.extend(entry_messaging_keys)
        if len(items) + len(messaging_keys) > MAX_WEBHOOK_ITEMS:
            raise MetaWebhookSanitizationError(
                "webhook item count exceeds the local limit"
            )
    return SanitizedWebhook(
        object_type=object_type,
        envelope={
            "schema_version": META_WEBHOOK_SCHEMA_VERSION,
            "object": object_type,
            "entry": sanitized_entries,
        },
        items=tuple(items),
        expected_messaging_keys=tuple(messaging_keys),
    )

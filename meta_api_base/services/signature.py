import hashlib
import hmac
import re

META_GRAPH_BASELINE_VERSION = "v26.0"
MAX_WEBHOOK_BODY_BYTES = 2 * 1024 * 1024

_SIGNATURE_PATTERN = re.compile(r"^sha256=([0-9a-fA-F]{64})$")
_GRAPH_VERSION_PATTERN = re.compile(r"^v[1-9][0-9]{0,2}\.0$")


def normalized_header(headers, name):
    """Return one case-insensitive header without changing its value."""

    target = str(name or "").lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == target:
            return str(value or "").strip()
    return ""


def verify_signature(secret, headers, body):
    """Validate Meta's HMAC-SHA256 signature over the exact request bytes."""

    signature = normalized_header(headers, "x-hub-signature-256")
    match = _SIGNATURE_PATTERN.fullmatch(signature)
    try:
        secret_bytes = secret.encode("utf-8")
    except (AttributeError, UnicodeEncodeError):
        secret_bytes = b""
    if (
        not match
        or not isinstance(secret, str)
        or not secret
        or not secret_bytes
        or len(secret_bytes) > 64 * 1024
        or not isinstance(body, (bytes, bytearray))
        or len(body) > MAX_WEBHOOK_BODY_BYTES
    ):
        return False
    expected = hmac.new(secret_bytes, bytes(body), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, match.group(1).lower())


def validate_graph_version(value):
    """Return whether ``value`` is one supported explicit Graph API version."""

    return bool(isinstance(value, str) and _GRAPH_VERSION_PATTERN.fullmatch(value))

import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from .errors import GoogleApiError, GoogleApiLimitError, GoogleApiTransientError


def normalized_header(headers, name):
    target = str(name or "").lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == target:
            return str(value or "").strip()
    return ""


def retry_after_seconds(headers, *, now=None):
    """Return a bounded Retry-After delta for integer and HTTP-date formats."""

    value = normalized_header(headers, "Retry-After")
    try:
        integer_retry = int(value)
    except (TypeError, ValueError):
        integer_retry = None
    if integer_retry is not None:
        return max(0, min(integer_retry, 86_400))
    if not value:
        return 0
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        reference = now or datetime.now(timezone.utc)
        return max(0, min(int((parsed - reference).total_seconds()), 86_400))
    except (OverflowError, TypeError, ValueError):
        return 0


def validate_response_limit(value, *, maximum=32 * 1024 * 1024):
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1024 <= value <= maximum
    ):
        raise GoogleApiLimitError("Google API response limit is invalid")
    return value


def bounded_json(response, maximum):
    """Decode one streamed JSON object under an enforced byte ceiling."""

    maximum = validate_response_limit(maximum)
    declared = normalized_header(getattr(response, "headers", {}), "Content-Length")
    if declared:
        try:
            if int(declared) > maximum:
                raise GoogleApiLimitError("Google API response is too large")
        except ValueError:
            declared = ""
    chunks = []
    size = 0
    stream_failed = False
    try:
        for chunk in response.iter_content(chunk_size=16 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > maximum:
                raise GoogleApiLimitError("Google API response is too large")
            chunks.append(chunk)
    except requests.RequestException:
        stream_failed = True
    if stream_failed:
        raise GoogleApiTransientError("Google API response stream was interrupted")
    raw = b"".join(chunks)
    invalid_payload = object()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        payload = invalid_payload
    if payload is invalid_payload:
        raise GoogleApiError("Google API response is invalid")
    if not isinstance(payload, dict):
        raise GoogleApiError("Google API response is invalid")
    return payload

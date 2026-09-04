import re

import requests

from .errors import (
    GoogleApiError,
    GoogleApiPausedError,
    GoogleApiPermissionError,
    GoogleApiRateLimitError,
    GoogleApiTransientError,
    safe_provider_reason,
    safe_provider_status,
    safe_request_id,
)
from .http import bounded_json, normalized_header, retry_after_seconds
from .version import validate_api_version

_GOOGLE_ADS_BASE_URL = "https://googleads.googleapis.com"
_GOOGLE_ADS_TIMEOUT = (5, 30)
_LIST_RESPONSE_LIMIT = 512 * 1024
_SEARCH_RESPONSE_LIMIT = 16 * 1024 * 1024
_CUSTOMER_ID_PATTERN = re.compile(r"^[0-9]{10}$")
_MAX_QUERY_BYTES = 64 * 1024
_MAX_PAGE_TOKEN_BYTES = 4 * 1024
_PROTOBUF_DURATION_PATTERN = re.compile(r"^([0-9]{1,6})(?:\.([0-9]{1,9}))?s$")


def normalize_customer_id(value, *, required=True):
    """Normalize the public Google Ads customer identifier for headers/paths."""

    value = str(value or "").strip().replace("-", "")
    if not value and not required:
        return ""
    if not _CUSTOMER_ID_PATTERN.fullmatch(value):
        raise GoogleApiError("Google Ads customer ID is invalid")
    return value


def _validated_query(value):
    if not isinstance(value, str):
        raise GoogleApiError("Google Ads query is invalid")
    value = value.strip()
    encoded = None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        encoded = None
    if encoded is None:
        raise GoogleApiError("Google Ads query is invalid")
    size = len(encoded)
    if not value or size > _MAX_QUERY_BYTES or "\x00" in value:
        raise GoogleApiError("Google Ads query is invalid")
    return value


def _validated_page_token(value):
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise GoogleApiError("Google Ads page token is invalid")
    encoded = None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        encoded = None
    if encoded is None:
        raise GoogleApiError("Google Ads page token is invalid")
    size = len(encoded)
    if (
        not value
        or size > _MAX_PAGE_TOKEN_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise GoogleApiError("Google Ads page token is invalid")
    return value


def _runtime_identity(identity):
    ensure_one = getattr(identity, "ensure_one", None)
    if not callable(ensure_one):
        raise GoogleApiError("Google API runtime identity is invalid")
    invalid_runtime = False
    try:
        ensured = ensure_one()
    except (AttributeError, TypeError, ValueError):
        invalid_runtime = True
        ensured = None
    if invalid_runtime:
        raise GoogleApiError("Google API runtime identity is invalid")
    identity = ensured if ensured is not None else identity
    required = ("active", "api_version", "developer_token", "login_customer_id")
    if any(not hasattr(identity, field_name) for field_name in required):
        raise GoogleApiError("Google API runtime identity is invalid")
    if not identity.active:
        raise GoogleApiPausedError("Google API identity is paused")
    if not validate_api_version(identity.api_version):
        raise GoogleApiPausedError("Google Ads API version is unsupported")
    if not isinstance(identity.developer_token, str) or not identity.developer_token:
        raise GoogleApiPausedError("Google Ads developer token is unavailable")
    encoded_token = None
    try:
        encoded_token = identity.developer_token.encode("utf-8")
    except UnicodeEncodeError:
        encoded_token = None
    if encoded_token is None:
        raise GoogleApiPausedError("Google Ads developer token is unavailable")
    if not 8 <= len(encoded_token) <= 64 * 1024 or any(
        ord(character) < 32 or ord(character) == 127
        for character in identity.developer_token
    ):
        raise GoogleApiPausedError("Google Ads developer token is unavailable")
    normalize_customer_id(identity.login_customer_id, required=False)
    return identity


def _request_id(response, payload):
    header = safe_request_id(
        normalized_header(getattr(response, "headers", {}), "request-id")
    )
    if header:
        return header
    error = payload.get("error") if isinstance(payload, dict) else None
    details = error.get("details") if isinstance(error, dict) else None
    if not isinstance(details, list):
        return ""
    for detail in details[:16]:
        if not isinstance(detail, dict):
            continue
        candidate = safe_request_id(detail.get("requestId"))
        if candidate:
            return candidate
    return ""


def _quota_retry_delay(item):
    """Read only the bounded protobuf Duration from quota error details."""

    details = item.get("details") if isinstance(item, dict) else None
    quota_details = (
        details.get("quotaErrorDetails") if isinstance(details, dict) else None
    )
    value = quota_details.get("retryDelay") if isinstance(quota_details, dict) else None
    match = (
        _PROTOBUF_DURATION_PATTERN.fullmatch(value) if isinstance(value, str) else None
    )
    if not match:
        return 0
    seconds = int(match.group(1))
    fraction = match.group(2) or ""
    if fraction and any(character != "0" for character in fraction):
        seconds += 1
    return max(0, min(seconds, 86_400))


def _provider_error(payload):
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return "UNKNOWN", "", 0
    status = safe_provider_status(error.get("status"))
    details = error.get("details")
    if not isinstance(details, list):
        return status, "", 0
    reason = ""
    retry_delay = 0
    for detail in details[:16]:
        if not isinstance(detail, dict):
            continue
        errors = detail.get("errors")
        if not isinstance(errors, list):
            continue
        for item in errors[:32]:
            retry_delay = retry_delay or _quota_retry_delay(item)
            error_code = item.get("errorCode") if isinstance(item, dict) else None
            if not isinstance(error_code, dict):
                continue
            for value in list(error_code.values())[:8]:
                candidate = safe_provider_reason(value)
                if candidate and not reason:
                    reason = candidate
            if reason and retry_delay:
                return status, reason, retry_delay
    return status, reason, retry_delay


def _quota_retry_fallback(reason):
    if reason == "EXCESSIVE_SHORT_TERM_QUERY_RESOURCE_CONSUMPTION":
        return 300
    if reason == "EXCESSIVE_LONG_TERM_QUERY_RESOURCE_CONSUMPTION":
        return 1_800
    return 60


def _raise_ads_error(response, payload, *, operation, api_version):
    status_code = int(getattr(response, "status_code", 0) or 0)
    provider_status, provider_reason, payload_retry = _provider_error(payload)
    header_retry = retry_after_seconds(getattr(response, "headers", {}))
    values = {
        "http_status": status_code,
        "request_id": _request_id(response, payload),
        "provider_status": provider_status,
        "provider_reason": provider_reason,
        "operation": operation,
        "api_version": api_version,
    }
    if status_code == 429 or provider_status == "RESOURCE_EXHAUSTED":
        raise GoogleApiRateLimitError(
            "Google Ads quota is exhausted",
            retry_after_seconds=(
                header_retry or payload_retry or _quota_retry_fallback(provider_reason)
            ),
            **values,
        )
    if status_code == 401 or provider_status == "UNAUTHENTICATED":
        raise GoogleApiPausedError("Google Ads authentication is unavailable", **values)
    if status_code == 403 or provider_status == "PERMISSION_DENIED":
        raise GoogleApiPermissionError("Google Ads permission is unavailable", **values)
    if (
        status_code in (408, 425)
        or status_code >= 500
        or provider_status
        in {"ABORTED", "DEADLINE_EXCEEDED", "INTERNAL", "UNAVAILABLE"}
    ):
        raise GoogleApiTransientError(
            "Google Ads is temporarily unavailable",
            retry_after_seconds=header_retry,
            **values,
        )
    raise GoogleApiError("Google Ads rejected the request", **values)


def _headers(identity, access_token, *, include_login_customer):
    if not isinstance(access_token, str) or not access_token:
        raise GoogleApiPausedError("Google OAuth access token is unavailable")
    encoded_token = None
    try:
        encoded_token = access_token.encode("utf-8")
    except UnicodeEncodeError:
        encoded_token = None
    if encoded_token is None:
        raise GoogleApiPausedError("Google OAuth access token is unavailable")
    if not 16 <= len(encoded_token) <= 64 * 1024 or any(
        ord(character) < 32 or ord(character) == 127 for character in access_token
    ):
        raise GoogleApiPausedError("Google OAuth access token is unavailable")
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer %s" % access_token,
        "developer-token": identity.developer_token,
    }
    if include_login_customer and identity.login_customer_id:
        headers["login-customer-id"] = normalize_customer_id(identity.login_customer_id)
    return headers


def _request(
    identity,
    access_token,
    *,
    operation,
    method,
    url,
    json_data=None,
    include_login_customer=False,
    max_response_bytes,
):
    identity = _runtime_identity(identity)
    response = None
    try:
        response = requests.request(
            method,
            url,
            headers=_headers(
                identity,
                access_token,
                include_login_customer=include_login_customer,
            ),
            json=json_data,
            timeout=_GOOGLE_ADS_TIMEOUT,
            allow_redirects=False,
            stream=True,
        )
    except requests.RequestException:
        response = None
    if response is None:
        raise GoogleApiTransientError(
            "Google Ads endpoint did not respond",
            operation=operation,
            api_version=identity.api_version,
        )
    try:
        status = int(getattr(response, "status_code", 0) or 0)
        if status in (401, 403, 408, 425, 429) or status >= 500:
            try:
                error_payload = bounded_json(response, max_response_bytes)
            except GoogleApiError:
                error_payload = {}
            _raise_ads_error(
                response,
                error_payload,
                operation=operation,
                api_version=identity.api_version,
            )
        payload = bounded_json(response, max_response_bytes)
        if not 200 <= status < 300 or isinstance(payload.get("error"), dict):
            _raise_ads_error(
                response,
                payload,
                operation=operation,
                api_version=identity.api_version,
            )
        return payload, _request_id(response, payload)
    finally:
        response.close()


def list_accessible_customers_request(identity, access_token):
    """Call the fixed discovery endpoint; login-customer-id is intentionally omitted."""

    identity = _runtime_identity(identity)
    url = "%s/%s/customers:listAccessibleCustomers" % (
        _GOOGLE_ADS_BASE_URL,
        identity.api_version,
    )
    return _request(
        identity,
        access_token,
        operation="customers.list",
        method="GET",
        url=url,
        include_login_customer=False,
        max_response_bytes=_LIST_RESPONSE_LIMIT,
    )


def search_request(identity, access_token, customer_id, query, *, page_token=""):
    """Call the fixed read-only GoogleAdsService.Search endpoint."""

    identity = _runtime_identity(identity)
    customer_id = normalize_customer_id(customer_id)
    query = _validated_query(query)
    page_token = _validated_page_token(page_token)
    url = "%s/%s/customers/%s/googleAds:search" % (
        _GOOGLE_ADS_BASE_URL,
        identity.api_version,
        customer_id,
    )
    body = {"query": query}
    if page_token:
        body["pageToken"] = page_token
    return _request(
        identity,
        access_token,
        operation="google_ads.search",
        method="POST",
        url=url,
        json_data=body,
        include_login_customer=True,
        max_response_bytes=_SEARCH_RESPONSE_LIMIT,
    )

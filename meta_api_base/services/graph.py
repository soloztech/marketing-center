import hashlib
import hmac
import json
import re
from collections.abc import Mapping

import requests

from .errors import (
    MetaApiError,
    MetaApiPausedError,
    MetaApiRateLimitError,
    MetaApiTransientError,
    MetaApiUncertainError,
)
from .signature import validate_graph_version

_GRAPH_BASE_URL = "https://graph.facebook.com"
_GRAPH_TIMEOUT = (5, 20)
_GRAPH_PATH_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")
_RATE_LIMIT_CODES = {4, 17, 32, 613}
_AUTHENTICATION_CODES = {190}
_PERMISSION_CODES = {10, 102, 200}
_CALLER_FORBIDDEN_KEYS = {
    "access_token",
    "app_secret",
    "client_secret",
    "appsecret_proof",
}


def _header(headers, name):
    target = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == target:
            return str(value or "").strip()
    return ""


def _retry_after(response):
    try:
        return max(0, min(int(_header(response.headers, "Retry-After")), 3600))
    except (TypeError, ValueError):
        return 0


def graph_appsecret_proof(app_secret, access_token):
    """Return Meta's token-bound proof without persisting either credential."""

    if not isinstance(app_secret, str) or not app_secret:
        raise MetaApiPausedError("Meta App credentials are unavailable")
    if not isinstance(access_token, str) or not access_token:
        raise MetaApiPausedError("Meta authorization is unavailable")
    return hmac.new(
        app_secret.encode("utf-8"),
        access_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def graph_app_access_token(app):
    """Build the standard App token in memory; callers must never persist it."""

    app.ensure_one()
    if not app.external_app_id or not app.app_secret:
        raise MetaApiPausedError("Meta App credentials are unavailable")
    return "%s|%s" % (app.external_app_id, app.app_secret)


def _protected_key_present(value, *, depth=0, seen=None):
    """Reject credential keys recursively without retaining caller values."""

    if depth > 32:
        raise MetaApiError("Meta Graph request structure is too deep")
    if seen is None:
        seen = set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            raise MetaApiError("Meta Graph request structure is recursive")
        seen.add(identity)
        try:
            for key, item in value.items():
                if str(key).lower() in _CALLER_FORBIDDEN_KEYS:
                    return True
                if _protected_key_present(item, depth=depth + 1, seen=seen):
                    return True
        finally:
            seen.remove(identity)
    elif isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in seen:
            raise MetaApiError("Meta Graph request structure is recursive")
        seen.add(identity)
        try:
            for item in value:
                if _protected_key_present(item, depth=depth + 1, seen=seen):
                    return True
        finally:
            seen.remove(identity)
    return False


def _copy_request_mapping(value):
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MetaApiError("Meta Graph request parameters are invalid")
    if _protected_key_present(value):
        raise MetaApiError("Meta Graph credentials must use the protected channel")
    try:
        return dict(value)
    except (TypeError, ValueError):
        raise MetaApiError("Meta Graph request parameters are invalid") from None


def _validated_request(app, access_token, method, path, params, data, json_data):
    app.ensure_one()
    method = str(method or "").upper()
    if method not in {"GET", "POST", "DELETE"}:
        raise MetaApiError("Meta Graph method is unsupported")
    path = str(path or "").strip("/")
    if not _GRAPH_PATH_PATTERN.fullmatch(path) or any(
        segment in {".", ".."} for segment in path.split("/")
    ):
        raise MetaApiError("Meta Graph path is invalid")
    version = str(app.graph_version or "")
    if not validate_graph_version(version):
        raise MetaApiError("Meta Graph version is invalid")
    if not app.active:
        raise MetaApiPausedError("Meta App is paused")
    if not isinstance(access_token, str) or not access_token:
        raise MetaApiPausedError("Meta authorization is unavailable")
    params = _copy_request_mapping(params)
    data = _copy_request_mapping(data)
    json_data = _copy_request_mapping(json_data)
    if data and json_data:
        raise MetaApiError("Meta Graph form and JSON bodies are mutually exclusive")
    proof = graph_appsecret_proof(app.app_secret, access_token)
    if method == "GET":
        params["appsecret_proof"] = proof
    elif json_data:
        json_data["appsecret_proof"] = proof
    else:
        data["appsecret_proof"] = proof
    return method, path, params, data, json_data


def _bounded_json(response, maximum):
    declared = _header(getattr(response, "headers", {}), "Content-Length")
    if declared:
        try:
            if int(declared) > maximum:
                raise MetaApiError("Meta Graph response is too large")
        except ValueError:
            declared = ""
    chunks = []
    size = 0
    try:
        for chunk in response.iter_content(chunk_size=16 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > maximum:
                raise MetaApiError("Meta Graph response is too large")
            chunks.append(chunk)
    except requests.RequestException:
        # A requests exception may contain the complete prepared URL, including
        # protected query parameters. Never retain it as exception context.
        raise MetaApiTransientError(
            "Meta Graph response stream was interrupted"
        ) from None
    raw = b"".join(chunks)
    if not raw and int(getattr(response, "status_code", 0) or 0) == 204:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        # JSON decoder errors may embed response fragments. The stable technical
        # error is sufficient and keeps provider content out of exception chains.
        raise MetaApiError("Meta Graph response is invalid") from None
    if not isinstance(payload, dict):
        raise MetaApiError("Meta Graph response is invalid")
    return payload


def _graph_error_shape(payload):
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return 0, 0, False
    raw_code = error.get("code")
    try:
        code = int(raw_code)
    except (TypeError, ValueError):
        code = 0
    raw_subcode = error.get("error_subcode")
    try:
        subcode = int(raw_subcode)
    except (TypeError, ValueError):
        subcode = 0
    return code, subcode, error.get("is_transient") is True


def _raise_graph_error(response, payload, *, mutating):
    status = int(getattr(response, "status_code", 0) or 0)
    code, subcode, provider_transient = _graph_error_shape(payload)
    if status == 429 or code in _RATE_LIMIT_CODES:
        raise MetaApiRateLimitError(
            "Meta Graph rate limit is active",
            retry_after_seconds=_retry_after(response) or 60,
            http_status=status,
            provider_code=code,
            provider_subcode=subcode,
        )
    if (
        status in (401, 403)
        or code in _AUTHENTICATION_CODES
        or code in _PERMISSION_CODES
    ):
        raise MetaApiPausedError(
            "Meta Graph authorization is unavailable",
            http_status=status,
            provider_code=code,
            provider_subcode=subcode,
        )
    if status == 408:
        error_class = MetaApiUncertainError if mutating else MetaApiTransientError
        raise error_class(
            "Meta Graph request timed out",
            retry_after_seconds=_retry_after(response),
            http_status=status,
            provider_code=code,
            provider_subcode=subcode,
        )
    if status >= 500:
        error_class = MetaApiUncertainError if mutating else MetaApiTransientError
        raise error_class(
            "Meta Graph is temporarily unavailable",
            retry_after_seconds=_retry_after(response),
            http_status=status,
            provider_code=code,
            provider_subcode=subcode,
        )
    if provider_transient:
        raise MetaApiTransientError(
            "Meta Graph rejected a transient request",
            retry_after_seconds=_retry_after(response),
            http_status=status,
            provider_code=code,
            provider_subcode=subcode,
        )
    raise MetaApiError(
        "Meta Graph rejected the request",
        http_status=status,
        provider_code=code,
        provider_subcode=subcode,
    )


def graph_request(
    app,
    access_token,
    method,
    path,
    *,
    params=None,
    data=None,
    json_data=None,
    mutating=False,
    max_response_bytes=256 * 1024,
):
    """Perform one bounded, credential-safe Meta Graph request.

    Mutating requests classify network failures and HTTP 5xx as uncertain: Meta
    may have applied the mutation before the connection failed, and its Send API
    does not provide a generally usable idempotency key.
    """

    if (
        not isinstance(max_response_bytes, int)
        or isinstance(max_response_bytes, bool)
        or not 1024 <= max_response_bytes <= 1024 * 1024
    ):
        raise MetaApiError("Meta Graph response limit is invalid")
    method, path, params, data, json_data = _validated_request(
        app, access_token, method, path, params, data, json_data
    )
    effective_mutating = bool(mutating) or method in {"POST", "DELETE"}
    try:
        response = requests.request(
            method,
            "%s/%s/%s" % (_GRAPH_BASE_URL, app.graph_version, path),
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer %s" % access_token,
            },
            params=params or None,
            data=data or None,
            json=json_data or None,
            timeout=_GRAPH_TIMEOUT,
            allow_redirects=False,
            stream=True,
        )
    except requests.RequestException:
        error_class = (
            MetaApiUncertainError if effective_mutating else MetaApiTransientError
        )
        # Do not chain the requests exception: its text can contain the full
        # prepared URL, including protected query parameters.
        raise error_class("Meta Graph endpoint did not respond") from None
    try:
        status = int(getattr(response, "status_code", 0) or 0)
        # These statuses are self-describing. Classify them before decoding so an
        # empty/HTML proxy body cannot weaken authentication, throttling or an
        # uncertain mutation into a generic JSON failure.
        if status in (401, 408, 429) or status >= 500:
            _raise_graph_error(response, {}, mutating=effective_mutating)
        if status == 403:
            try:
                payload = _bounded_json(response, max_response_bytes)
            except (MetaApiError, MetaApiTransientError):
                _raise_graph_error(response, {}, mutating=effective_mutating)
            _raise_graph_error(response, payload, mutating=effective_mutating)
        try:
            payload = _bounded_json(response, max_response_bytes)
        except (MetaApiError, MetaApiTransientError) as error:
            if effective_mutating:
                raise MetaApiUncertainError(
                    "Meta Graph mutation response could not be verified"
                ) from error
            raise
        if not 200 <= int(response.status_code or 0) < 300:
            _raise_graph_error(response, payload, mutating=effective_mutating)
        if isinstance(payload.get("error"), dict):
            _raise_graph_error(response, payload, mutating=effective_mutating)
        return payload
    finally:
        response.close()


def graph_debug_token(app, input_token):
    """Inspect a token without putting either credential in the request path."""

    if not isinstance(input_token, str) or not input_token:
        raise MetaApiPausedError("Meta authorization is unavailable")
    app_token = graph_app_access_token(app)
    return graph_request(
        app,
        app_token,
        "GET",
        "debug_token",
        params={"input_token": input_token},
        max_response_bytes=64 * 1024,
    )

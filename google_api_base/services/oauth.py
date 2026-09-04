from collections.abc import Mapping

import requests

try:
    import google.auth.exceptions
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2 import service_account
except ImportError:  # pragma: no cover - enforced by the Odoo manifest
    google = None
    GoogleAuthRequest = None
    service_account = None

from .contracts import GoogleOAuthToken
from .errors import (
    GoogleApiError,
    GoogleApiPausedError,
    GoogleApiRateLimitError,
    GoogleApiTransientError,
    safe_request_id,
)
from .http import bounded_json, normalized_header, retry_after_seconds

_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
_OAUTH_TIMEOUT = (5, 20)
_OAUTH_RESPONSE_LIMIT = 64 * 1024
_OAUTH_PAUSED_ERRORS = {
    "invalid_client",
    "invalid_grant",
    "unauthorized_client",
}
_OAUTH_TRANSIENT_ERRORS = {"server_error", "temporarily_unavailable"}
_GOOGLE_ADS_SCOPE = "https://www.googleapis.com/auth/adwords"


class _BoundedGoogleAuthRequest(GoogleAuthRequest if GoogleAuthRequest else object):
    def __call__(
        self, url, method="GET", body=None, headers=None, timeout=120, **kwargs
    ):
        # The service-account token URI is allow-listed at credential loading,
        # and redirects must not widen that trust boundary.
        kwargs["allow_redirects"] = False
        return super().__call__(
            url=url,
            method=method,
            body=body,
            headers=headers,
            timeout=_OAUTH_TIMEOUT[1],
            **kwargs,
        )


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
    required = ("active", "auth_mode")
    if any(not hasattr(identity, field_name) for field_name in required):
        raise GoogleApiError("Google API runtime identity is invalid")
    if not identity.active:
        raise GoogleApiPausedError("Google API identity is paused")
    credential_fields = {
        "authorized_user": (
            "oauth_client_id",
            "oauth_client_secret",
            "refresh_token",
        ),
        "service_account": ("service_account_info",),
    }.get(identity.auth_mode)
    if credential_fields is None or any(
        not hasattr(identity, field_name) for field_name in credential_fields
    ):
        raise GoogleApiError("Google API runtime identity is invalid")
    if identity.auth_mode == "service_account":
        if not isinstance(identity.service_account_info, Mapping):
            raise GoogleApiPausedError("Google OAuth credentials are unavailable")
        return identity
    for field_name in credential_fields:
        value = getattr(identity, field_name)
        if not isinstance(value, str) or not value:
            raise GoogleApiPausedError("Google OAuth credentials are unavailable")
        encoded = None
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            encoded = None
        if encoded is None:
            raise GoogleApiPausedError("Google OAuth credentials are unavailable")
        if not 8 <= len(encoded) <= 64 * 1024 or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise GoogleApiPausedError("Google OAuth credentials are unavailable")
    return identity


def _valid_access_token(value):
    if not isinstance(value, str):
        return False
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return bool(
        16 <= len(encoded) <= 64 * 1024
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _oauth_error_code(payload):
    value = payload.get("error") if isinstance(payload, dict) else ""
    value = str(value or "").strip().lower()
    if value in _OAUTH_PAUSED_ERRORS | _OAUTH_TRANSIENT_ERRORS:
        return value
    return ""


def _raise_oauth_error(response, payload):
    status = int(getattr(response, "status_code", 0) or 0)
    request_id = safe_request_id(
        normalized_header(getattr(response, "headers", {}), "request-id")
    )
    retry_after = retry_after_seconds(getattr(response, "headers", {}))
    provider_error = _oauth_error_code(payload)
    values = {
        "http_status": status,
        "request_id": request_id,
        "operation": "oauth.refresh",
    }
    if status == 429:
        raise GoogleApiRateLimitError(
            "Google OAuth rate limit is active",
            retry_after_seconds=retry_after or 60,
            provider_status="RESOURCE_EXHAUSTED",
            **values,
        )
    if (
        status in (408, 425)
        or status >= 500
        or provider_error in _OAUTH_TRANSIENT_ERRORS
    ):
        raise GoogleApiTransientError(
            "Google OAuth is temporarily unavailable",
            retry_after_seconds=retry_after,
            provider_status="UNAVAILABLE",
            **values,
        )
    if status in (400, 401, 403) or provider_error in _OAUTH_PAUSED_ERRORS:
        raise GoogleApiPausedError(
            "Google OAuth authorization is unavailable",
            provider_status="UNAUTHENTICATED",
            **values,
        )
    raise GoogleApiError("Google OAuth rejected the request", **values)


def _refresh_service_account(identity):
    if service_account is None or GoogleAuthRequest is None:
        raise GoogleApiPausedError(
            "Google service account runtime is unavailable",
            operation="oauth.service_account",
        )
    credentials = None
    try:
        credentials = service_account.Credentials.from_service_account_info(
            dict(identity.service_account_info),
            scopes=(_GOOGLE_ADS_SCOPE,),
        )
        credentials.refresh(_BoundedGoogleAuthRequest())
    except google.auth.exceptions.TransportError:
        raise GoogleApiTransientError(
            "Google OAuth endpoint did not respond",
            operation="oauth.service_account",
        ) from None
    except (google.auth.exceptions.GoogleAuthError, ValueError, TypeError):
        raise GoogleApiPausedError(
            "Google service account authorization is unavailable",
            provider_status="UNAUTHENTICATED",
            operation="oauth.service_account",
        ) from None
    access_token = getattr(credentials, "token", None)
    expiry = getattr(credentials, "expiry", None)
    expires_in = None
    try:
        import datetime

        now = datetime.datetime.now(datetime.timezone.utc)
        if expiry.tzinfo is None:
            now = now.replace(tzinfo=None)
        expires_in = int((expiry - now).total_seconds())
    except (AttributeError, OverflowError, TypeError, ValueError):
        expires_in = None
    if (
        not _valid_access_token(access_token)
        or expires_in is None
        or not 60 <= expires_in <= 86_400
    ):
        raise GoogleApiError(
            "Google OAuth response is invalid", operation="oauth.service_account"
        )
    return GoogleOAuthToken(access_token=access_token, expires_in_seconds=expires_in)


def refresh_access_token(identity):
    """Exchange the externally resolved refresh token for a short-lived token."""

    identity = _runtime_identity(identity)
    if identity.auth_mode == "service_account":
        return _refresh_service_account(identity)
    response = None
    try:
        response = requests.request(
            "POST",
            _OAUTH_TOKEN_URL,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "client_id": identity.oauth_client_id,
                "client_secret": identity.oauth_client_secret,
                "refresh_token": identity.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=_OAUTH_TIMEOUT,
            allow_redirects=False,
            stream=True,
        )
    except requests.RequestException:
        response = None
    if response is None:
        raise GoogleApiTransientError(
            "Google OAuth endpoint did not respond", operation="oauth.refresh"
        )
    try:
        status = int(getattr(response, "status_code", 0) or 0)
        if status in (401, 403, 408, 425, 429) or status >= 500:
            _raise_oauth_error(response, {})
        payload = bounded_json(response, _OAUTH_RESPONSE_LIMIT)
        if not 200 <= status < 300:
            _raise_oauth_error(response, payload)
        access_token = payload.get("access_token")
        token_type = str(payload.get("token_type") or "").strip().lower()
        expires_in = payload.get("expires_in")
        if (
            not _valid_access_token(access_token)
            or token_type != "bearer"
            or isinstance(expires_in, bool)
        ):
            raise GoogleApiError(
                "Google OAuth response is invalid", operation="oauth.refresh"
            )
        invalid_expiry = False
        try:
            expires_in = int(expires_in)
        except (OverflowError, TypeError, ValueError):
            invalid_expiry = True
        if invalid_expiry:
            raise GoogleApiError(
                "Google OAuth response is invalid", operation="oauth.refresh"
            )
        if not 60 <= expires_in <= 86_400:
            raise GoogleApiError(
                "Google OAuth response is invalid", operation="oauth.refresh"
            )
        return GoogleOAuthToken(
            access_token=access_token, expires_in_seconds=expires_in
        )
    finally:
        response.close()

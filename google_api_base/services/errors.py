import re

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:/=+-]{1,128}$")
_SAFE_PROVIDER_STATUSES = frozenset(
    {
        "ABORTED",
        "ALREADY_EXISTS",
        "CANCELLED",
        "DATA_LOSS",
        "DEADLINE_EXCEEDED",
        "FAILED_PRECONDITION",
        "INTERNAL",
        "INVALID_ARGUMENT",
        "NOT_FOUND",
        "OUT_OF_RANGE",
        "PERMISSION_DENIED",
        "RESOURCE_EXHAUSTED",
        "UNAUTHENTICATED",
        "UNAVAILABLE",
        "UNKNOWN",
        "UNIMPLEMENTED",
    }
)
_SAFE_PROVIDER_REASONS = frozenset(
    {
        "AUTHENTICATION_ERROR",
        "CLIENT_CUSTOMER_ID_INVALID",
        "CUSTOMER_NOT_ENABLED",
        "CUSTOMER_NOT_FOUND",
        "DEVELOPER_TOKEN_INVALID",
        "DEVELOPER_TOKEN_NOT_APPROVED",
        "DEVELOPER_TOKEN_NOT_ON_ALLOWLIST",
        "DEVELOPER_TOKEN_PROHIBITED",
        "EXCESSIVE_LONG_TERM_QUERY_RESOURCE_CONSUMPTION",
        "EXCESSIVE_SHORT_TERM_QUERY_RESOURCE_CONSUMPTION",
        "GOOGLE_ACCOUNT_COOKIE_INVALID",
        "INCOMPLETE_SIGNUP",
        "INVALID_LOGIN_CUSTOMER_ID_SERVING_CUSTOMER_ID_COMBINATION",
        "METRIC_ACCESS_DENIED",
        "MISSING_TOS",
        "OAUTH_TOKEN_EXPIRED",
        "OAUTH_TOKEN_INVALID",
        "PROJECT_DISABLED",
        "RESOURCE_EXHAUSTED",
        "RESOURCE_TEMPORARILY_EXHAUSTED",
        "SERVICE_ACCESS_DENIED",
        "USER_PERMISSION_DENIED",
    }
)


def safe_request_id(value):
    """Allow-list provider request identifiers before they enter diagnostics."""

    value = str(value or "").strip()
    return value if _REQUEST_ID_PATTERN.fullmatch(value) else ""


def safe_provider_status(value):
    value = str(value or "").strip().upper()
    return value if value in _SAFE_PROVIDER_STATUSES else "UNKNOWN"


def safe_provider_reason(value):
    value = str(value or "").strip().upper()
    return value if value in _SAFE_PROVIDER_REASONS else ""


class GoogleApiError(Exception):
    """Permanent, safely reportable failure at the Google API boundary."""

    classification = "permanent"

    def __init__(
        self,
        message,
        *,
        retry_after_seconds=0,
        http_status=0,
        request_id="",
        provider_status="UNKNOWN",
        provider_reason="",
        operation="",
        api_version="",
    ):
        super().__init__(message)
        self.retry_after_seconds = _bounded_nonnegative_int(
            retry_after_seconds, maximum=86_400
        )
        self.http_status = _bounded_nonnegative_int(http_status, maximum=599)
        self.request_id = safe_request_id(request_id)
        self.provider_status = safe_provider_status(provider_status)
        self.provider_reason = safe_provider_reason(provider_reason)
        self.operation = _safe_context(operation, maximum=48)
        self.api_version = _safe_context(api_version, maximum=8)


class GoogleApiTransientError(GoogleApiError):
    """Temporary read failure for which a bounded retry is appropriate."""

    classification = "transient"


class GoogleApiPausedError(GoogleApiError):
    """Credential or configuration failure that pauses provider use."""

    classification = "paused"


class GoogleApiPermissionError(GoogleApiPausedError):
    """Authorization scope or account access is unavailable."""

    classification = "permission_denied"


class GoogleApiRateLimitError(GoogleApiTransientError):
    """Authoritative quota response carrying a bounded cooldown."""

    classification = "rate_limited"


class GoogleApiLimitError(GoogleApiError):
    """A local request/response bound was exceeded."""

    classification = "limit_exceeded"


def _bounded_nonnegative_int(value, *, maximum):
    try:
        number = int(value)
    except (OverflowError, TypeError, ValueError):
        return 0
    if isinstance(value, bool):
        return 0
    return max(0, min(number, maximum))


def _safe_context(value, *, maximum):
    value = str(value or "").strip()
    if not value or len(value) > maximum:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
        return ""
    return value

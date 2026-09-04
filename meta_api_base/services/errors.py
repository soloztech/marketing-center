class MetaApiError(Exception):
    """Permanent, safely reportable failure at the Meta API boundary."""

    classification = "permanent"

    def __init__(
        self,
        message,
        *,
        retry_after_seconds=0,
        http_status=0,
        provider_code=0,
        provider_subcode=0,
    ):
        super().__init__(message)
        self.retry_after_seconds = _bounded_nonnegative_int(
            retry_after_seconds, maximum=86_400
        )
        self.http_status = _bounded_nonnegative_int(http_status, maximum=599)
        self.provider_code = _bounded_nonnegative_int(
            provider_code, maximum=2_147_483_647
        )
        self.provider_subcode = _bounded_nonnegative_int(
            provider_subcode, maximum=2_147_483_647
        )


class MetaApiTransientError(MetaApiError):
    """Temporary failure for which a bounded retry may be appropriate."""

    classification = "transient"


class MetaApiUncertainError(MetaApiError):
    """Mutation whose remote outcome cannot be proven locally."""

    classification = "uncertain"


class MetaApiPausedError(MetaApiError):
    """Authentication or configuration failure that pauses provider use."""

    classification = "paused"


class MetaApiRateLimitError(MetaApiTransientError):
    """Authoritative throttling response that is safe to retry after cooldown."""

    classification = "rate_limited"


def _bounded_nonnegative_int(value, *, maximum):
    try:
        number = int(value)
    except (OverflowError, TypeError, ValueError):
        return 0
    if isinstance(value, bool):
        return 0
    return max(0, min(number, maximum))

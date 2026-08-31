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
        self.retry_after_seconds = retry_after_seconds
        self.http_status = http_status
        self.provider_code = provider_code
        self.provider_subcode = provider_subcode


class MetaApiTransientError(MetaApiError):
    """Temporary failure for which a bounded retry may be appropriate."""

    classification = "transient"


class MetaApiUncertainError(MetaApiError):
    """Mutation whose remote outcome cannot be proven locally."""

    classification = "uncertain"


class MetaApiPausedError(MetaApiError):
    """Authentication or configuration failure that pauses provider use."""

    classification = "paused"


class MetaApiRateLimitError(MetaApiPausedError):
    """Authoritative throttling response that is safe to retry after cooldown."""

    classification = "rate_limited"

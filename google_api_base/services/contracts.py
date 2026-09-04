import copy
import dataclasses


@dataclasses.dataclass(frozen=True)
class GoogleOAuthToken:
    """Short-lived OAuth token that must remain process-local."""

    access_token: str = dataclasses.field(repr=False, compare=False)
    expires_in_seconds: int

    def __reduce__(self):
        raise TypeError("Google OAuth token is not serializable")

    def __reduce_ex__(self, _protocol):
        raise TypeError("Google OAuth token is not serializable")


@dataclasses.dataclass(frozen=True)
class GoogleAccessibleCustomers:
    """Technical discovery result; no business account is created here."""

    resource_names: tuple
    request_id: str = ""


@dataclasses.dataclass(frozen=True)
class GoogleSearchPage:
    """One provider-neutral page of generic Google Ads query rows."""

    results: tuple = dataclasses.field(repr=False)
    next_page_token: str = dataclasses.field(default="", repr=False)
    request_id: str = ""
    field_mask: str = ""
    total_results_count: int = 0

    @classmethod
    def from_values(
        cls,
        *,
        results,
        next_page_token="",
        request_id="",
        field_mask="",
        total_results_count=0,
    ):
        """Detach rows from the mutable decoded transport payload."""

        return cls(
            results=tuple(copy.deepcopy(row) for row in results),
            next_page_token=next_page_token,
            request_id=request_id,
            field_mask=field_mask,
            total_results_count=total_results_count,
        )

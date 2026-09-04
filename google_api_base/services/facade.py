import re
import time

from .contracts import GoogleAccessibleCustomers, GoogleSearchPage
from .errors import GoogleApiError, GoogleApiLimitError
from .oauth import refresh_access_token
from .transport import (
    list_accessible_customers_request,
    normalize_customer_id,
    search_request,
)

_RESOURCE_NAME_PATTERN = re.compile(r"^customers/[0-9]{10}$")
_MAX_ACCESSIBLE_CUSTOMERS = 10_000
_MAX_RESULTS_PER_PAGE = 10_000
_MAX_QUERY_BYTES = 64 * 1024
_MAX_PAGE_TOKEN_BYTES = 4 * 1024


def _query(value):
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
        raise GoogleApiLimitError("Google Ads query exceeds the local limit")
    return value


def _page_token(value):
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
    if not value or size > _MAX_PAGE_TOKEN_BYTES:
        raise GoogleApiLimitError("Google Ads page token exceeds the local limit")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise GoogleApiError("Google Ads page token is invalid")
    return value


def _positive_bound(value, *, name, maximum):
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= maximum
    ):
        raise GoogleApiLimitError("Google Ads %s limit is invalid" % name)
    return value


def _accessible_customers(payload, request_id):
    values = payload.get("resourceNames", [])
    if not isinstance(values, list) or len(values) > _MAX_ACCESSIBLE_CUSTOMERS:
        raise GoogleApiError("Google Ads customer discovery response is invalid")
    resource_names = []
    for value in values:
        if not isinstance(value, str) or not _RESOURCE_NAME_PATTERN.fullmatch(value):
            raise GoogleApiError("Google Ads customer discovery response is invalid")
        resource_names.append(value)
    if len(set(resource_names)) != len(resource_names):
        raise GoogleApiError("Google Ads customer discovery response is invalid")
    return GoogleAccessibleCustomers(tuple(resource_names), request_id=request_id)


def _search_page(payload, request_id):
    results = payload.get("results", [])
    if not isinstance(results, list) or len(results) > _MAX_RESULTS_PER_PAGE:
        raise GoogleApiError("Google Ads search response is invalid")
    if any(not isinstance(row, dict) for row in results):
        raise GoogleApiError("Google Ads search response is invalid")
    next_page_token = _page_token(payload.get("nextPageToken", ""))
    raw_field_mask = payload.get("fieldMask", "")
    if not isinstance(raw_field_mask, str):
        raise GoogleApiError("Google Ads search response is invalid")
    field_mask = raw_field_mask
    try:
        field_mask_bytes = field_mask.encode("utf-8")
    except UnicodeEncodeError:
        field_mask_bytes = b""
    if (
        (not field_mask_bytes and field_mask)
        or len(field_mask_bytes) > 16 * 1024
        or any(ord(character) < 32 or ord(character) == 127 for character in field_mask)
    ):
        raise GoogleApiError("Google Ads search response is invalid")
    total = payload.get("totalResultsCount", 0)
    invalid_total = False
    try:
        total = int(total)
    except (OverflowError, TypeError, ValueError):
        invalid_total = True
    if invalid_total:
        raise GoogleApiError("Google Ads search response is invalid")
    if isinstance(payload.get("totalResultsCount"), bool) or not 0 <= total <= 10**12:
        raise GoogleApiError("Google Ads search response is invalid")
    return GoogleSearchPage.from_values(
        results=results,
        next_page_token=next_page_token,
        request_id=request_id,
        field_mask=field_mask,
        total_results_count=total,
    )


class GoogleAdsFacade:
    """Domain-neutral, read-only facade over the official Google Ads REST API."""

    def __init__(self, identity):
        self._identity = identity

    def list_accessible_customers(self):
        token = refresh_access_token(self._identity)
        payload, request_id = list_accessible_customers_request(
            self._identity, token.access_token
        )
        return _accessible_customers(payload, request_id)

    def search_page(self, customer_id, query, *, page_token=""):
        customer_id = normalize_customer_id(customer_id)
        query = _query(query)
        page_token = _page_token(page_token)
        token = refresh_access_token(self._identity)
        payload, request_id = search_request(
            self._identity,
            token.access_token,
            customer_id,
            query,
            page_token=page_token,
        )
        return _search_page(payload, request_id)

    def iter_search_pages(
        self,
        customer_id,
        query,
        *,
        max_pages=10,
        max_rows=100_000,
        max_elapsed_seconds=240,
    ):
        """Yield bounded pages while reusing one short-lived OAuth token."""

        customer_id = normalize_customer_id(customer_id)
        query = _query(query)
        max_pages = _positive_bound(max_pages, name="page", maximum=100)
        max_rows = _positive_bound(max_rows, name="row", maximum=1_000_000)
        max_elapsed_seconds = _positive_bound(
            max_elapsed_seconds, name="elapsed-time", maximum=900
        )
        started_at = time.monotonic()
        token = refresh_access_token(self._identity)
        effective_deadline = min(
            max_elapsed_seconds,
            max(1, token.expires_in_seconds - 30),
        )
        page_token = ""
        seen_tokens = set()
        row_count = 0
        for page_number in range(1, max_pages + 1):
            if time.monotonic() - started_at >= effective_deadline:
                raise GoogleApiLimitError("Google Ads elapsed-time limit was exceeded")
            payload, request_id = search_request(
                self._identity,
                token.access_token,
                customer_id,
                query,
                page_token=page_token,
            )
            page = _search_page(payload, request_id)
            if time.monotonic() - started_at >= effective_deadline:
                raise GoogleApiLimitError("Google Ads elapsed-time limit was exceeded")
            row_count += len(page.results)
            if row_count > max_rows:
                raise GoogleApiLimitError("Google Ads row limit was exceeded")
            yield page
            next_token = page.next_page_token
            if not next_token:
                return
            if next_token in seen_tokens or next_token == page_token:
                raise GoogleApiError("Google Ads pagination did not advance")
            seen_tokens.add(next_token)
            page_token = next_token
            if page_number == max_pages:
                raise GoogleApiLimitError("Google Ads page limit was exceeded")

import datetime
import re

import pytz

from odoo.addons.google_api_base.services.errors import GoogleApiError
from odoo.addons.marketing_center_base.services.performance_dto import (
    MarketingPerformanceDTO,
    PerformancePageDTO,
)
from odoo.addons.marketing_center_base.services.timezone import (
    LocalDateBoundaryError,
    local_date_boundary_utc,
)

from .catalog import normalize_customer_id

GOOGLE_PERFORMANCE_CONTRACT_VERSION = "google.ads.performance.daily.v25.1"
GOOGLE_PERFORMANCE_GRAINS = (
    "account",
    "campaign",
    "ad_group",
    "ad",
    "keyword",
)

_RESOURCE_PATTERNS = {
    "account": re.compile(r"^customers/[0-9]{10}$"),
    "campaign": re.compile(r"^customers/[0-9]{10}/campaigns/[0-9]+$"),
    "ad_group": re.compile(r"^customers/[0-9]{10}/adGroups/[0-9]+$"),
    "ad": re.compile(r"^customers/[0-9]{10}/adGroupAds/[0-9]+~[0-9]+$"),
    "keyword": re.compile(r"^customers/[0-9]{10}/adGroupCriteria/[0-9]+~[0-9]+$"),
}
_ROW_RESOURCE = {
    "account": ("customer", "resourceName"),
    "campaign": ("campaign", "resourceName"),
    "ad_group": ("adGroup", "resourceName"),
    "ad": ("adGroupAd", "resourceName"),
    "keyword": ("adGroupCriterion", "resourceName"),
}
_FROM_RESOURCE = {
    "account": "customer",
    "campaign": "campaign",
    "ad_group": "ad_group",
    "ad": "ad_group_ad",
    "keyword": "keyword_view",
}
_SELECT_RESOURCE = {
    "account": "customer.resource_name",
    "campaign": "campaign.resource_name",
    "ad_group": "ad_group.resource_name",
    "ad": "ad_group_ad.resource_name",
    "keyword": "ad_group_criterion.resource_name",
}
_ORDER_RESOURCE = {
    "account": "customer.id",
    "campaign": "campaign.id",
    "ad_group": "ad_group.id",
    "ad": "ad_group_ad.ad.id",
    "keyword": "ad_group_criterion.criterion_id",
}
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_MAX_PAGE_TOKEN_BYTES = 3072
_PERFORMANCE_CHUNK_SIZE = 200
_MAX_BIGINT = 2**63 - 1


def google_performance_query(grain, date_from, date_to):
    grain = _grain(grain)
    date_from = _date(date_from, "performance start date")
    date_to = _date(date_to, "performance end date")
    if date_from > date_to or (date_to - date_from).days >= 31:
        raise GoogleApiError("Google Ads performance window is invalid")
    return " ".join(
        (
            "SELECT",
            _SELECT_RESOURCE[grain] + ",",
            "segments.date, metrics.impressions, metrics.clicks, metrics.cost_micros",
            "FROM",
            _FROM_RESOURCE[grain],
            "WHERE segments.date BETWEEN '%s' AND '%s'"
            % (date_from.isoformat(), date_to.isoformat()),
            "ORDER BY segments.date,",
            _ORDER_RESOURCE[grain],
        )
    )


def google_performance_reporting_context(
    customer_id,
    grain,
    currency,
    report_timezone,
):
    customer_id = normalize_customer_id(customer_id)
    grain = _grain(grain)
    currency = _currency(currency)
    report_timezone = _timezone(report_timezone)
    return {
        "provider": "google_ads",
        "api_version": "v25",
        "contract_version": GOOGLE_PERFORMANCE_CONTRACT_VERSION,
        "customer_ref": "customers/%s" % customer_id,
        "grain": grain,
        "currency": currency,
        "report_timezone": report_timezone,
        "date_mode": "interaction_date",
        "metrics": ["clicks", "cost_micros", "impressions"],
        "dimensions": [],
        "query_hash_policy": "fixed_allowlist",
    }


def normalize_performance_window(date_from, date_to, report_timezone):
    date_from = _date(date_from, "performance start date")
    date_to = _date(date_to, "performance end date")
    report_timezone = _timezone(report_timezone)
    if date_from > date_to or (date_to - date_from).days >= 31:
        raise GoogleApiError("Google Ads performance window is invalid")
    try:
        start_utc = local_date_boundary_utc(date_from, report_timezone)
        end_utc = local_date_boundary_utc(
            date_to + datetime.timedelta(days=1), report_timezone
        )
    except (LocalDateBoundaryError, OverflowError):
        raise GoogleApiError("Google Ads performance window is invalid") from None
    return date_from, date_to, start_utc, end_utc


def performance_window_from_utc(window_start, window_end, report_timezone):
    report_timezone = _timezone(report_timezone)
    if (
        not isinstance(window_start, datetime.datetime)
        or not isinstance(window_end, datetime.datetime)
        or window_start.tzinfo
        or window_end.tzinfo
    ):
        raise GoogleApiError("Google Ads performance run window is invalid")
    zone = pytz.timezone(report_timezone)
    start_local = pytz.UTC.localize(window_start).astimezone(zone)
    end_local = pytz.UTC.localize(window_end).astimezone(zone)
    if start_local.date() >= end_local.date():
        raise GoogleApiError("Google Ads performance run window is invalid")
    date_from = start_local.date()
    date_to = end_local.date() - datetime.timedelta(days=1)
    normalized = normalize_performance_window(date_from, date_to, report_timezone)
    if normalized[2] != window_start or normalized[3] != window_end:
        raise GoogleApiError("Google Ads performance run window is invalid")
    return date_from, date_to


def normalize_google_performance_page(
    customer_id,
    grain,
    page,
    *,
    current_page_token="",
    date_from,
    date_to,
    currency,
    report_timezone,
    reporting_context_hash,
    observed_at=None,
):
    """Normalize a fixed Google Search page into bounded performance pages."""

    customer_id = normalize_customer_id(customer_id)
    grain = _grain(grain)
    date_from, date_to, _start, _end = normalize_performance_window(
        date_from, date_to, report_timezone
    )
    currency = _currency(currency)
    current_page_token = _page_token(current_page_token)
    rows = getattr(page, "results", None)
    if not isinstance(rows, tuple) or len(rows) > 10_000:
        raise GoogleApiError("Google Ads performance page is invalid")
    observed_at = _observed_at(observed_at)
    items = tuple(
        _performance_row(
            row,
            customer_id=customer_id,
            grain=grain,
            date_from=date_from,
            date_to=date_to,
            currency=currency,
            report_timezone=report_timezone,
            reporting_context_hash=reporting_context_hash,
            observed_at=observed_at,
        )
        for row in rows
    )
    next_token = _page_token(getattr(page, "next_page_token", ""))
    if next_token and next_token == current_page_token:
        raise GoogleApiError("Google Ads performance pagination made no progress")
    chunks = [
        items[index : index + _PERFORMANCE_CHUNK_SIZE]
        for index in range(0, len(items), _PERFORMANCE_CHUNK_SIZE)
    ] or [()]
    pages = []
    for index, chunk in enumerate(chunks):
        last = index == len(chunks) - 1
        pages.append(
            PerformancePageDTO(
                items=tuple(chunk),
                next_cursor=next_token if last else current_page_token or "__first__",
                has_more=bool(next_token) if last else True,
                provider_request_id=getattr(page, "request_id", ""),
                watermark=date_to.isoformat(),
                reporting_context_hash=reporting_context_hash,
            )
        )
    return tuple(pages)


def validate_google_performance_cursor(value):
    return _page_token(value)


def _performance_row(
    row,
    *,
    customer_id,
    grain,
    date_from,
    date_to,
    currency,
    report_timezone,
    reporting_context_hash,
    observed_at,
):
    if not isinstance(row, dict):
        raise GoogleApiError("Google Ads performance row is invalid")
    row_key, resource_key = _ROW_RESOURCE[grain]
    resource = row.get(row_key)
    if not isinstance(resource, dict):
        raise GoogleApiError("Google Ads performance resource is invalid")
    external_ref = resource.get(resource_key)
    if (
        not isinstance(external_ref, str)
        or not _RESOURCE_PATTERNS[grain].fullmatch(external_ref)
        or not external_ref.startswith("customers/%s" % customer_id)
    ):
        raise GoogleApiError("Google Ads performance resource is invalid")
    segments = row.get("segments")
    metrics = row.get("metrics")
    if not isinstance(segments, dict) or not isinstance(metrics, dict):
        raise GoogleApiError("Google Ads performance row is invalid")
    report_date = _date(segments.get("date"), "performance report date")
    if report_date < date_from or report_date > date_to:
        raise GoogleApiError("Google Ads performance row is outside its window")
    impressions = _counter(metrics.get("impressions"), "impressions")
    clicks = _counter(metrics.get("clicks"), "clicks")
    cost_micros = _counter(metrics.get("costMicros"), "cost micros")
    try:
        period_start = local_date_boundary_utc(report_date, report_timezone)
        period_end = local_date_boundary_utc(
            report_date + datetime.timedelta(days=1), report_timezone
        )
    except (LocalDateBoundaryError, OverflowError):
        raise GoogleApiError("Google Ads performance date is invalid") from None
    return MarketingPerformanceDTO(
        grain=grain,
        entity_external_ref=external_ref,
        report_date=report_date,
        period_start_utc=period_start,
        period_end_utc=period_end,
        report_timezone=report_timezone,
        currency=currency,
        observed_at=observed_at,
        impressions=impressions,
        clicks=clicks,
        cost_micros=cost_micros,
        dimensions={},
        metric_origin="platform_reported",
        reporting_context_hash=reporting_context_hash,
        source_schema_version=GOOGLE_PERFORMANCE_CONTRACT_VERSION,
    )


def _grain(value):
    value = str(value or "").strip().lower()
    if value not in GOOGLE_PERFORMANCE_GRAINS:
        raise GoogleApiError("Google Ads performance grain is unsupported")
    return value


def _date(value, label):
    if isinstance(value, datetime.datetime):
        raise GoogleApiError("Google Ads %s is invalid" % label)
    if isinstance(value, datetime.date):
        return value
    if not isinstance(value, str):
        raise GoogleApiError("Google Ads %s is invalid" % label)
    try:
        parsed = datetime.date.fromisoformat(value)
    except ValueError:
        raise GoogleApiError("Google Ads %s is invalid" % label) from None
    if parsed.isoformat() != value:
        raise GoogleApiError("Google Ads %s is invalid" % label)
    return parsed


def _currency(value):
    value = str(value or "").strip().upper()
    if not _CURRENCY_RE.fullmatch(value):
        raise GoogleApiError("Google Ads currency is invalid")
    return value


def _timezone(value):
    value = str(value or "").strip()
    if value not in pytz.all_timezones_set:
        raise GoogleApiError("Google Ads timezone is invalid")
    return value


def _counter(value, label):
    if isinstance(value, bool):
        raise GoogleApiError("Google Ads %s is invalid" % label)
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isdigit():
        result = int(value)
    else:
        raise GoogleApiError("Google Ads %s is invalid" % label)
    if result < 0 or result > _MAX_BIGINT:
        raise GoogleApiError("Google Ads %s is invalid" % label)
    return result


def _page_token(value):
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise GoogleApiError("Google Ads performance cursor is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise GoogleApiError("Google Ads performance cursor is invalid") from None
    if len(encoded) > _MAX_PAGE_TOKEN_BYTES or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise GoogleApiError("Google Ads performance cursor is invalid")
    return value


def _observed_at(value):
    value = value or datetime.datetime.utcnow()
    if not isinstance(value, datetime.datetime):
        raise GoogleApiError("Google Ads observation timestamp is invalid")
    if value.tzinfo:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)

import datetime
import json
import re
from decimal import Decimal, InvalidOperation

import pytz

from odoo.addons.marketing_center_base.services.performance_dto import (
    MarketingPerformanceDTO,
    PerformancePageDTO,
)
from odoo.addons.marketing_center_base.services.timezone import (
    LocalDateBoundaryError,
    local_date_boundary_utc,
)
from odoo.addons.meta_api_base.services.errors import MetaApiError
from odoo.addons.meta_api_base.services.graph import graph_request

from .graph_contract import require_marketing_graph_version

META_INSIGHTS_CONTRACT_VERSION = "meta.marketing.insights.daily.v1"
META_INSIGHTS_GRAINS = ("account", "campaign")

_ACCOUNT_FIELDS = (
    "account_id",
    "account_currency",
    "date_start",
    "date_stop",
    "impressions",
    "clicks",
    "spend",
)
_CAMPAIGN_FIELDS = _ACCOUNT_FIELDS + ("campaign_id", "campaign_name")
_ACCOUNT_REF_RE = re.compile(r"^act_[0-9]+$")
_OBJECT_ID_RE = re.compile(r"^[0-9]+$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_MAX_CURSOR_BYTES = 3072
_MAX_PAGE_ITEMS = 200
_PAGE_LIMIT = 100
_MAX_WINDOW_DAYS = 31
_MAX_BIGINT = 2**63 - 1
_MICROS_PER_UNIT = Decimal("1000000")


def meta_insights_fields(grain):
    grain = _grain(grain)
    return _ACCOUNT_FIELDS if grain == "account" else _CAMPAIGN_FIELDS


def meta_insights_reporting_context(
    graph_version,
    account_ref,
    grain,
    currency,
    report_timezone,
):
    graph_version = _graph_version(graph_version)
    account_ref = _account_ref(account_ref)
    grain = _grain(grain)
    currency = _currency(currency)
    report_timezone = _timezone(report_timezone)
    return {
        "provider": "meta",
        "graph_version": graph_version,
        "contract_version": META_INSIGHTS_CONTRACT_VERSION,
        "account_ref": account_ref,
        "edge": "insights",
        "grain": grain,
        "level": grain,
        "fields": list(meta_insights_fields(grain)),
        "dimensions": [],
        "time_increment": 1,
        "actions_policy": "disabled",
        "attribution_policy": "disabled",
        "breakdowns": [],
        "filtering": [],
        "currency": currency,
        "report_timezone": report_timezone,
    }


def normalize_insights_window(date_from, date_to, report_timezone):
    date_from = _date(date_from, "window start")
    date_to = _date(date_to, "window end")
    report_timezone = _timezone(report_timezone)
    day_count = (date_to - date_from).days + 1
    if day_count < 1 or day_count > _MAX_WINDOW_DAYS:
        raise MetaApiError("Meta Insights window is invalid")
    start_utc = _local_midnight_utc(date_from, report_timezone)
    end_utc = _local_midnight_utc(
        date_to + datetime.timedelta(days=1),
        report_timezone,
    )
    return date_from, date_to, start_utc, end_utc


def insights_window_from_utc(window_start, window_end, report_timezone):
    report_timezone = _timezone(report_timezone)
    if not isinstance(window_start, datetime.datetime) or not isinstance(
        window_end, datetime.datetime
    ):
        raise MetaApiError("Meta Insights run window is invalid")
    if window_start.tzinfo or window_end.tzinfo:
        raise MetaApiError("Meta Insights run window is invalid")
    zone = pytz.timezone(report_timezone)
    start_local = pytz.UTC.localize(window_start).astimezone(zone)
    end_local = pytz.UTC.localize(window_end).astimezone(zone)
    if start_local.date() >= end_local.date():
        raise MetaApiError("Meta Insights run window is invalid")
    date_from = start_local.date()
    date_to = end_local.date() - datetime.timedelta(days=1)
    normalized = normalize_insights_window(date_from, date_to, report_timezone)
    if normalized[2] != window_start or normalized[3] != window_end:
        raise MetaApiError("Meta Insights run window is invalid")
    return date_from, date_to


def fetch_meta_insights_page(
    app,
    access_token,
    account_ref,
    grain,
    *,
    date_from,
    date_to,
    currency,
    report_timezone,
    after="",
    reporting_context_hash,
    observed_at=None,
):
    """Fetch and normalize one bounded, strictly daily Insights page."""

    _graph_version(app.graph_version)
    account_ref = _account_ref(account_ref)
    grain = _grain(grain)
    currency = _currency(currency)
    report_timezone = _timezone(report_timezone)
    date_from, date_to, _window_start, _window_end = normalize_insights_window(
        date_from,
        date_to,
        report_timezone,
    )
    after = _bounded_text(
        after,
        "Insights cursor",
        _MAX_CURSOR_BYTES,
        required=False,
    )
    params = {
        "fields": ",".join(meta_insights_fields(grain)),
        "level": grain,
        "time_increment": 1,
        "time_range": json.dumps(
            {
                "since": date_from.isoformat(),
                "until": date_to.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        "limit": _PAGE_LIMIT,
    }
    if after:
        params["after"] = after
    payload = graph_request(
        app,
        access_token,
        "GET",
        "%s/insights" % account_ref,
        params=params,
        max_response_bytes=1024 * 1024,
    )
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > _MAX_PAGE_ITEMS:
        raise MetaApiError("Meta Insights page is invalid")
    observed_at = _observed_at(observed_at)
    try:
        items = tuple(
            _performance_row(
                row,
                account_ref=account_ref,
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
        next_cursor, has_more = _next_cursor(payload, after)
        return PerformancePageDTO(
            items=items,
            next_cursor=next_cursor,
            has_more=has_more,
            watermark=date_to.isoformat(),
            reporting_context_hash=reporting_context_hash,
        )
    except MetaApiError:
        raise
    except (TypeError, ValueError):
        raise MetaApiError("Meta Insights page normalization failed") from None


def _performance_row(
    row,
    *,
    account_ref,
    grain,
    date_from,
    date_to,
    currency,
    report_timezone,
    reporting_context_hash,
    observed_at,
):
    if not isinstance(row, dict):
        raise MetaApiError("Meta Insights row is invalid")
    account_id = _object_id(row.get("account_id"), "Insights account ID")
    if account_ref != "act_%s" % account_id:
        raise MetaApiError("Meta Insights account identity is inconsistent")
    row_currency = _currency(row.get("account_currency"))
    if row_currency != currency:
        raise MetaApiError("Meta Insights currency is inconsistent")
    report_date = _date(row.get("date_start"), "Insights start date")
    if report_date != _date(row.get("date_stop"), "Insights stop date"):
        raise MetaApiError("Meta Insights row is not daily")
    if report_date < date_from or report_date > date_to:
        raise MetaApiError("Meta Insights row is outside the requested window")

    entity_external_ref = account_ref
    if grain == "campaign":
        campaign_id = _object_id(row.get("campaign_id"), "campaign ID")
        entity_external_ref = "%s/campaigns/%s" % (account_ref, campaign_id)
        campaign_name = row.get("campaign_name")
        if campaign_name not in (None, ""):
            _bounded_text(campaign_name, "campaign name", 1024)

    cost_micros = _cost_micros(row["spend"]) if "spend" in row else None
    impressions = (
        _count(row["impressions"], "impressions") if "impressions" in row else None
    )
    clicks = _count(row["clicks"], "clicks") if "clicks" in row else None
    if cost_micros is None and impressions is None and clicks is None:
        raise MetaApiError("Meta Insights row has no supported metrics")

    period_start_utc = _local_midnight_utc(report_date, report_timezone)
    period_end_utc = _local_midnight_utc(
        report_date + datetime.timedelta(days=1),
        report_timezone,
    )
    return MarketingPerformanceDTO(
        grain=grain,
        entity_external_ref=entity_external_ref,
        report_date=report_date,
        period_start_utc=period_start_utc,
        period_end_utc=period_end_utc,
        report_timezone=report_timezone,
        currency=currency,
        observed_at=observed_at,
        impressions=impressions,
        clicks=clicks,
        cost_micros=cost_micros,
        dimensions={},
        metric_origin="platform_reported",
        reporting_context_hash=reporting_context_hash,
        source_schema_version=META_INSIGHTS_CONTRACT_VERSION,
    )


def _next_cursor(payload, current_cursor):
    raw_paging = payload.get("paging")
    if raw_paging not in (None, False) and not isinstance(raw_paging, dict):
        raise MetaApiError("Meta Insights pagination is invalid")
    paging = raw_paging or {}
    raw_cursors = paging.get("cursors")
    if raw_cursors not in (None, False) and not isinstance(raw_cursors, dict):
        raise MetaApiError("Meta Insights pagination is invalid")
    next_page = paging.get("next")
    if next_page in (None, False, ""):
        return "", False
    if not isinstance(next_page, str):
        raise MetaApiError("Meta Insights pagination is invalid")
    next_cursor = _bounded_text(
        (raw_cursors or {}).get("after"),
        "Insights cursor",
        _MAX_CURSOR_BYTES,
        required=False,
    )
    if not next_cursor or next_cursor == current_cursor:
        raise MetaApiError("Meta Insights pagination is invalid")
    return next_cursor, True


def _cost_micros(value):
    if not isinstance(value, str):
        raise MetaApiError("Meta Insights spend is invalid")
    value = _bounded_text(value, "Insights spend", 64)
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        raise MetaApiError("Meta Insights spend is invalid") from None
    if not amount.is_finite() or amount < 0:
        raise MetaApiError("Meta Insights spend is invalid")
    if amount > Decimal(_MAX_BIGINT) / _MICROS_PER_UNIT:
        raise MetaApiError("Meta Insights spend is invalid")
    micros = amount * _MICROS_PER_UNIT
    if micros != micros.to_integral_value():
        raise MetaApiError("Meta Insights spend precision is unsupported")
    result = int(micros)
    if result > _MAX_BIGINT:
        raise MetaApiError("Meta Insights spend is invalid")
    return result


def _count(value, label):
    if isinstance(value, bool):
        raise MetaApiError("Meta Insights %s is invalid" % label)
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isdigit():
        result = int(value)
    else:
        raise MetaApiError("Meta Insights %s is invalid" % label)
    if result < 0 or result > _MAX_BIGINT:
        raise MetaApiError("Meta Insights %s is invalid" % label)
    return result


def _local_midnight_utc(value, report_timezone):
    try:
        return local_date_boundary_utc(value, report_timezone)
    except LocalDateBoundaryError:
        raise MetaApiError("Meta Insights local date boundary is invalid") from None


def _date(value, label):
    if isinstance(value, datetime.datetime):
        raise MetaApiError("Meta %s is invalid" % label)
    if isinstance(value, datetime.date):
        return value
    value = _bounded_text(value, label, 10)
    try:
        result = datetime.date.fromisoformat(value)
    except ValueError:
        raise MetaApiError("Meta %s is invalid" % label) from None
    if result.isoformat() != value:
        raise MetaApiError("Meta %s is invalid" % label)
    return result


def _graph_version(value):
    return require_marketing_graph_version(value, "insights")


def _grain(value):
    value = _bounded_text(value, "Insights grain", 32).lower()
    if value not in META_INSIGHTS_GRAINS:
        raise MetaApiError("Meta Insights grain is unsupported")
    return value


def _account_ref(value):
    value = _bounded_text(value, "ad account reference", 64)
    if not _ACCOUNT_REF_RE.fullmatch(value):
        raise MetaApiError("Meta ad account reference is invalid")
    return value


def _object_id(value, label):
    value = _bounded_text(value, label, 64)
    if not _OBJECT_ID_RE.fullmatch(value):
        raise MetaApiError("Meta %s is invalid" % label)
    return value


def _currency(value):
    value = _bounded_text(value, "Insights currency", 3).upper()
    if not _CURRENCY_RE.fullmatch(value):
        raise MetaApiError("Meta Insights currency is invalid")
    return value


def _timezone(value):
    value = _bounded_text(value, "Insights timezone", 64)
    if value not in pytz.all_timezones_set:
        raise MetaApiError("Meta Insights timezone is invalid")
    return value


def _bounded_text(value, label, limit, required=True):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise MetaApiError("Meta %s is invalid" % label)
    value = value.strip()
    if (required and not value) or len(value) > limit:
        raise MetaApiError("Meta %s is invalid" % label)
    if any(ord(character) < 32 for character in value):
        raise MetaApiError("Meta %s is invalid" % label)
    return value


def _observed_at(value):
    if value is None:
        value = datetime.datetime.utcnow()
    if not isinstance(value, datetime.datetime):
        raise MetaApiError("Meta Insights observation time is invalid")
    if value.tzinfo:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)

"""An exact, bounded ClickView query; raw click IDs never leave this boundary."""

import dataclasses
import datetime
import re

import pytz

from odoo.addons.google_api_base.services.errors import GoogleApiError
from odoo.addons.google_api_base.services.facade import normalize_customer_id

# Identifiers arrive URL-decoded from ingress. Restrict the literal alphabet so
# neither quotes nor GAQL syntax can be interpolated into the equality filter.
_GCLID_RE = re.compile(r"^[A-Za-z0-9_.~-]{1,2048}$")
_ID_RE = re.compile(r"^[0-9]+$")
_ENUM_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


@dataclasses.dataclass(frozen=True)
class GoogleClickMatch:
    assets: dict
    details: dict


def click_acquisition_at(occurred_at, assets):
    """Keep the captured visit date when a form is submitted on a later day."""
    raw = (assets or {}).get("acquisition_at")
    if not raw:
        return occurred_at
    try:
        if not isinstance(raw, str):
            raise ValueError()
        instant = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if instant.tzinfo is None or instant.utcoffset() != datetime.timedelta(0):
            raise ValueError()
        instant = instant.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        event = occurred_at
        if event.tzinfo:
            event = event.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        if instant > event:
            raise ValueError()
        return instant
    except (ValueError, TypeError, AttributeError):
        raise GoogleApiError("Google acquisition date is invalid") from None


def click_local_date(occurred_at, timezone, *, now=None):
    try:
        zone = pytz.timezone(timezone)
        instant = occurred_at
        current = now or datetime.datetime.utcnow()
        if not isinstance(instant, datetime.datetime):
            raise ValueError()
        instant = pytz.UTC.localize(instant) if not instant.tzinfo else instant
        current = pytz.UTC.localize(current) if not current.tzinfo else current
        local_date = instant.astimezone(zone).date()
        age = (current.astimezone(zone).date() - local_date).days
        if not 0 <= age <= 90:
            raise ValueError()
        return local_date
    except (ValueError, TypeError, pytz.UnknownTimeZoneError):
        raise GoogleApiError(
            "Google click date is outside the supported account window"
        ) from None


def click_query(gclid, local_date):
    if not isinstance(gclid, str) or not _GCLID_RE.fullmatch(gclid):
        raise GoogleApiError("Google click identifier format is unsupported")
    if type(local_date) is not datetime.date:
        raise GoogleApiError("Google click local date is invalid")
    return (
        " ".join(
            """SELECT customer.id, customer.time_zone, segments.date,
                  click_view.gclid, click_view.ad_group_ad,
                  click_view.keyword, click_view.keyword_info.text,
                  click_view.keyword_info.match_type,
                  campaign.resource_name, campaign.id, campaign.name,
                  ad_group.resource_name, ad_group.id, ad_group.name,
                  segments.ad_network_type, segments.device
             FROM click_view
            WHERE segments.date = '%s' AND click_view.gclid = '%s'
            LIMIT 2""".split()
        )
        % (local_date.isoformat(), gclid)
    )


def normalize_click_page(page, customer_id, gclid, local_date, timezone):
    """Return only allowlisted fields after exact account/date/GCLID matching."""
    customer_id = normalize_customer_id(customer_id)
    if page.next_page_token or len(page.results) > 1:
        raise GoogleApiError("Google click lookup has multiple matches")
    if not page.results:
        return None
    row = page.results[0]
    if not isinstance(row, dict):
        raise GoogleApiError("Google click response is invalid")
    customer = row.get("customer") or {}
    click = row.get("clickView") or {}
    segments = row.get("segments") or {}
    campaign = row.get("campaign") or {}
    group = row.get("adGroup") or {}
    if (
        str(customer.get("id")) != customer_id
        or customer.get("timeZone") != timezone
        or click.get("gclid") != gclid
        or segments.get("date") != local_date.isoformat()
    ):
        raise GoogleApiError("Google click response does not match its query scope")
    campaign_ref = _resource(campaign, customer_id, "campaigns")
    assets = {"google.customer_id": customer_id, "google.campaign_id": campaign_ref}
    details = {"campaign_name": _text(campaign.get("name"), 1024)}
    if group.get("id"):
        assets["google.ad_group_id"] = _resource(group, customer_id, "adGroups")
        details["ad_group_name"] = _text(group.get("name"), 1024)
    ad = click.get("adGroupAd")
    if ad:
        match = re.fullmatch(r"customers/([0-9]{10})/adGroupAds/([0-9]+)~([0-9]+)", ad)
        if not match or match[1] != customer_id or match[2] != str(group.get("id")):
            raise GoogleApiError("Google click ad scope is inconsistent")
        assets["google.ad_id"] = ad
    keyword = click.get("keyword")
    if keyword:
        match = re.fullmatch(
            r"customers/([0-9]{10})/adGroupCriteria/([0-9]+)~([0-9]+)", keyword
        )
        if not match or match[1] != customer_id or match[2] != str(group.get("id")):
            raise GoogleApiError("Google click keyword scope is inconsistent")
        details["keyword_ref"] = keyword
    info = click.get("keywordInfo") or {}
    details["keyword_text"] = _text(info.get("text"), 1024)
    for key, value in (
        ("keyword_match_type", info.get("matchType")),
        ("network", segments.get("adNetworkType")),
        ("device", segments.get("device")),
    ):
        if value:
            if not isinstance(value, str) or not _ENUM_RE.fullmatch(value):
                raise GoogleApiError("Google click enumeration is invalid")
            details[key] = value
    return GoogleClickMatch(assets=assets, details=details)


def _resource(value, customer_id, segment):
    identifier = str(value.get("id") or "")
    expected = "customers/%s/%s/%s" % (customer_id, segment, identifier)
    if not _ID_RE.fullmatch(identifier) or value.get("resourceName") != expected:
        raise GoogleApiError("Google click campaign/group scope is inconsistent")
    return expected


def _text(value, limit):
    if value is None:
        return ""
    if (
        not isinstance(value, str)
        or len(value) > limit
        or any(ord(c) < 32 for c in value)
    ):
        raise GoogleApiError("Google click response text is invalid")
    return value

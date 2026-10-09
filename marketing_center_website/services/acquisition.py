"""Select navigation evidence once; authorization belongs to each consumer."""

import datetime
import re
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

QUERY_FIELDS = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "gbraid",
    "wbraid",
    "fbclid",
    "gad_campaignid",
    "gad_source",
)
CLICK_FIELDS = {"gclid", "gbraid", "wbraid", "fbclid"}
_TECHNICAL = ("/web", "/website", "/marketing", "/my", "/portal", "/auth")


def safe_page(value, origin):
    """Exact same-origin public HTTPS URL, stripped of query and fragment."""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or urlunsplit((parsed.scheme, parsed.netloc, "", "", "")) != origin
            or len(value) > 8192
        ):
            return ""
        path = unquote(parsed.path or "/").lower()
        if any(
            path == prefix or path.startswith(prefix + "/") for prefix in _TECHNICAL
        ):
            return ""
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))
    except (TypeError, ValueError):
        return ""


def acquisition_values(url):
    try:
        query = parse_qs(urlsplit(url).query, max_num_fields=100)
    except (TypeError, ValueError):
        return {}
    values = {}
    for key in QUERY_FIELDS:
        candidates = query.get(key, [])
        if len(candidates) != 1:
            continue
        value = candidates[0].strip()
        if not value or len(value) > 512 or re.search(r"[\x00-\x1f\x7f]", value):
            continue
        if key in CLICK_FIELDS and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:~-]{0,511}", value
        ):
            continue
        if key in {"gad_campaignid", "gad_source"} and not re.fullmatch(
            r"[0-9]{1,32}", value
        ):
            continue
        values[key] = value
    return values


def utc_iso(value):
    return value.replace(tzinfo=datetime.timezone.utc, microsecond=0).isoformat()


def resolve_acquisition(
    env, website, origin, referrer, now, *, visitor=None, cookies=None, purpose
):
    """R1 matrix: visit evidence never gives an old cookie a click timestamp.

    A partial referrer/track tuple owns its origin. History is usable only up
    to a matching current-page anchor, ordered by (time, id). The explicit
    purpose retains the form's direct-visit timestamp for provenance ``none``.
    Callers persist this result in their own immutable event, never on retry.
    """
    if purpose not in {"form", "whatsapp"}:
        raise ValueError("Unknown acquisition purpose")
    page_url = safe_page(referrer, origin)
    if not page_url:
        return {}
    values = acquisition_values(referrer)
    provenance = "referrer" if values else "none"
    landing_url = page_url
    track = env["website.track"]
    acquired_at = visit_at = None
    if visitor and visitor.exists():
        tracks = (
            env["website.track"]
            .sudo()
            .search(
                [
                    ("visitor_id", "=", visitor.id),
                    ("visit_datetime", "<=", now),
                    ("visit_datetime", ">=", now - datetime.timedelta(hours=24)),
                    ("url", "=like", origin + "/%"),
                    "|",
                    ("page_id.website_id", "=", False),
                    ("page_id.website_id", "=", website.id),
                ],
                order="visit_datetime desc, id desc",
                limit=200,
            )
            .filtered(lambda item: safe_page(item.url, origin))
        )
        anchor = next(
            (
                item
                for item in tracks
                if safe_page(item.url, origin) == page_url
                and (not values or acquisition_values(item.url) == values)
            ),
            None,
        )
        if anchor:
            track = anchor
            visit_at = anchor.visit_datetime
            if values:
                acquired_at = visit_at
            else:
                chosen = next(
                    (
                        item
                        for item in tracks
                        if (item.visit_datetime, item.id)
                        <= (anchor.visit_datetime, anchor.id)
                        and acquisition_values(item.url)
                    ),
                    None,
                )
                if chosen:
                    track = chosen
                    values = acquisition_values(chosen.url)
                    landing_url = safe_page(chosen.url, origin)
                    acquired_at = chosen.visit_datetime
                    provenance = "track"
    if not values:
        for suffix in ("source", "medium", "campaign"):
            value = unquote((cookies or {}).get("odoo_utm_" + suffix, ""))
            if value and len(value) <= 512 and not re.search(r"[\x00-\x1f\x7f]", value):
                values["utm_" + suffix] = value
        if values:
            provenance = "cookie"
        elif purpose == "form":
            acquired_at = visit_at
    return {
        "page_url": page_url,
        "landing_url": landing_url,
        "values": values,
        "track": track,
        "acquired_at": acquired_at,
        "visit_at": visit_at,
        "provenance": provenance,
    }

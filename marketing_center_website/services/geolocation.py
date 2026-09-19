"""Small, bounded parser for origin-sanitized IP/location observations."""

import ipaddress
import re
import unicodedata
from urllib.parse import unquote

import pytz


def ip_address(value):
    if not isinstance(value, str) or len(value) > 45 or "%" in value:
        return None
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    return getattr(address, "ipv4_mapped", None) or address


def trusted_peer(environ, networks):
    # REMOTE_ADDR after ProxyFix can come from X-Forwarded-For. Trust the socket
    # address saved BEFORE that middleware, never a forwarded address.
    original = environ.get("werkzeug.proxy_fix.orig", {})
    peer = ip_address(original.get("REMOTE_ADDR", environ.get("REMOTE_ADDR")))
    if not peer:
        return False
    try:
        allowed = [
            ipaddress.ip_network(value.strip(), strict=False)
            for value in networks.split(",")
            if value.strip()
        ]
    except (AttributeError, ValueError):
        return False
    return any(peer in network for network in allowed)


def _decode_header(value):
    # WSGI represents header bytes as Latin-1; Cloudflare can send UTF-8 city
    # names. Repair only when that byte sequence is valid UTF-8.
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def _text(value, limit):
    if not isinstance(value, str) or len(value) > limit * 3:
        return ""
    value = _decode_header(unquote(value).strip())
    if len(value) > limit or any(
        unicodedata.category(c).startswith("C") for c in value
    ):
        return ""
    return value


def _location(values):
    country = _text(values.get("country_code"), 2).upper()
    if not re.fullmatch("[A-Z]{2}", country) or country in {"XX", "T1"}:
        return {}
    region = _text(values.get("region"), 16).upper()
    if not re.fullmatch("[A-Z0-9-]{1,16}", region):
        region = ""
    timezone = _text(values.get("time_zone"), 64)
    return {
        "country_code": country,
        "region": region,
        "city": _text(values.get("city"), 128),
        "timezone": timezone if timezone in pytz.all_timezones_set else "",
    }


def request_observation(environ, headers, networks, geoip_lookup):
    """Ignore raw CF/XFF headers; the configured origin must overwrite X-MC-*.

    Private IPs are retained as observations without location guesses. If a
    trusted origin supplies an invalid address, do not mislabel its own address
    as the visitor. Native GeoIP uses the current address, not session cache.
    """
    trusted = trusted_peer(environ, networks)
    original = environ.get("werkzeug.proxy_fix.orig", {})
    address = ip_address(
        headers.get("X-MC-Visitor-IP")
        if trusted
        else original.get("REMOTE_ADDR", environ.get("REMOTE_ADDR"))
    )
    if not address or address.is_unspecified or address.is_multicast:
        return {}
    result = {"ip": str(address)}
    if not address.is_global:
        return result
    if trusted and headers.get("X-MC-Geo-Source") == "cloudflare":
        location = _location(
            {
                "country_code": headers.get("X-MC-Geo-Country"),
                "region": headers.get("X-MC-Geo-Region"),
                "city": headers.get("X-MC-Geo-City"),
                "time_zone": headers.get("X-MC-Geo-Timezone"),
            }
        )
        if location:
            return dict(result, source="cloudflare", **location)
    try:
        location = _location(geoip_lookup(str(address)) or {})
    except Exception:  # Optional local GeoIP must not lose an otherwise valid IP.
        location = {}
    return dict(result, source="geoip", **location) if location else result

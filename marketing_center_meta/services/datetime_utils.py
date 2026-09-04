import datetime
import re

_COMPACT_TIMEZONE_RE = re.compile(r"([+-][0-9]{2})([0-9]{2})$")


def parse_meta_datetime(value):
    """Parse Meta timestamps consistently on the Python 3.10 Odoo runtime."""

    if not isinstance(value, str):
        raise ValueError("Meta timestamp must be text")
    normalized = "%s+00:00" % value[:-1] if value.endswith("Z") else value
    normalized = _COMPACT_TIMEZONE_RE.sub(r"\1:\2", normalized)
    parsed = datetime.datetime.fromisoformat(normalized)
    if not parsed.tzinfo or parsed.utcoffset() is None:
        raise ValueError("Meta timestamp must include a timezone")
    return parsed.astimezone(datetime.timezone.utc).replace(
        tzinfo=None,
        microsecond=0,
    )

import datetime

import pytz


class LocalDateBoundaryError(ValueError):
    """A timezone cannot represent a deterministic boundary for one local date."""


def _earliest_utc(candidates):
    return min(candidates, key=lambda item: item.astimezone(pytz.UTC))


def first_valid_local_instant(report_date, timezone_name):
    """Return the first representable instant belonging to ``report_date``.

    Some IANA zones advance the clock at local midnight. ``pytz.localize`` then
    raises ``NonExistentTimeError`` even though the reporting day itself is valid.
    Ambiguous boundaries choose the chronologically first UTC occurrence; gaps move
    forward to the first valid minute of the same civil date.
    """

    if isinstance(report_date, datetime.datetime) or not isinstance(
        report_date, datetime.date
    ):
        raise LocalDateBoundaryError("report_date must be a date")
    try:
        zone = pytz.timezone(timezone_name)
    except (pytz.UnknownTimeZoneError, AttributeError, TypeError):
        raise LocalDateBoundaryError("report timezone is invalid") from None
    midnight = datetime.datetime.combine(report_date, datetime.time.min)
    try:
        return zone.localize(midnight, is_dst=None)
    except pytz.AmbiguousTimeError:
        return _earliest_utc(
            (
                zone.localize(midnight, is_dst=True),
                zone.localize(midnight, is_dst=False),
            )
        )
    except pytz.NonExistentTimeError:
        first_candidate_minute = 1

    # Bound the search to the same civil date: a date skipped in its entirety has
    # no valid daily reporting interval.
    for minute in range(first_candidate_minute, 24 * 60):
        candidate = midnight + datetime.timedelta(minutes=minute)
        try:
            return zone.localize(candidate, is_dst=None)
        except pytz.AmbiguousTimeError:
            return _earliest_utc(
                (
                    zone.localize(candidate, is_dst=True),
                    zone.localize(candidate, is_dst=False),
                )
            )
        except pytz.NonExistentTimeError:
            continue
    raise LocalDateBoundaryError("local reporting date does not exist")


def local_date_boundary_utc(report_date, timezone_name):
    """Return the first valid local instant as a naive UTC datetime."""

    return (
        first_valid_local_instant(report_date, timezone_name)
        .astimezone(pytz.UTC)
        .replace(tzinfo=None, microsecond=0)
    )

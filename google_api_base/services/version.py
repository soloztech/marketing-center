import re

GOOGLE_ADS_BASELINE_VERSION = "v25"
GOOGLE_ADS_SUPPORTED_VERSIONS = frozenset({GOOGLE_ADS_BASELINE_VERSION})

_VERSION_PATTERN = re.compile(r"^v[1-9][0-9]{0,2}$")


def validate_api_version(value):
    """Return whether ``value`` is an explicitly supported Google Ads version."""

    return bool(
        isinstance(value, str)
        and _VERSION_PATTERN.fullmatch(value)
        and value in GOOGLE_ADS_SUPPORTED_VERSIONS
    )

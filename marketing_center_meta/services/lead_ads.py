import datetime
import hashlib
import json
import re
from dataclasses import dataclass

from odoo.addons.marketing_center_base.services.dto import canonical_json
from odoo.addons.meta_api_base.services.errors import MetaApiError
from odoo.addons.meta_api_base.services.graph import graph_request

from .datetime_utils import parse_meta_datetime
from .graph_contract import require_marketing_graph_version

META_LEAD_CONTRACT_VERSION = "meta.lead_ads.graph.v26"
META_LEAD_FIELDS = "created_time,id,ad_id,campaign_id,form_id,field_data"

_ID_RE = re.compile(r"^[0-9]{1,40}$")
_MAX_CURSOR_BYTES = 3072
_MAX_FIELDS = 128
_MAX_VALUES_PER_FIELD = 32
_MAX_VALUE_LENGTH = 4096
_PAGE_LIMIT = 100


@dataclass(frozen=True)
class MetaLeadField:
    name: str
    values: tuple


@dataclass(frozen=True)
class MetaLead:
    leadgen_id: str
    form_id: str
    ad_id: str
    campaign_id: str
    created_at: datetime.datetime
    fields: tuple
    payload_sha256: str


@dataclass(frozen=True)
class MetaLeadPage:
    leads: tuple
    next_after: str
    has_more: bool


def fetch_meta_lead(app, access_token, leadgen_id):
    """Retrieve and normalize one lead; the webhook hint is never trusted as data."""

    _require_supported_graph_version(app)
    leadgen_id = _identifier(leadgen_id, "lead ID")
    payload = graph_request(
        app,
        access_token,
        "GET",
        leadgen_id,
        params={"fields": META_LEAD_FIELDS},
        max_response_bytes=512 * 1024,
    )
    return normalize_meta_lead(payload, expected_leadgen_id=leadgen_id)


def fetch_meta_lead_page(app, access_token, form_id, *, after="", since=None):
    """Retrieve exactly one bounded form page for reconciliation."""

    _require_supported_graph_version(app)
    form_id = _identifier(form_id, "form ID")
    after = _bounded_text(after, "lead cursor", _MAX_CURSOR_BYTES, required=False)
    params = {"fields": META_LEAD_FIELDS, "limit": _PAGE_LIMIT}
    if after:
        params["after"] = after
    if since is not None:
        params["filtering"] = json.dumps(
            [
                {
                    "field": "time_created",
                    "operator": "GREATER_THAN_OR_EQUAL",
                    "value": _unix_time(since),
                }
            ],
            separators=(",", ":"),
            sort_keys=True,
        )
    payload = graph_request(
        app,
        access_token,
        "GET",
        "%s/leads" % form_id,
        params=params,
        max_response_bytes=1024 * 1024,
    )
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > _PAGE_LIMIT:
        raise MetaApiError("Meta lead page is invalid")
    leads = tuple(normalize_meta_lead(row, expected_form_id=form_id) for row in rows)
    next_after, has_more = _next_cursor(payload, after)
    return MetaLeadPage(leads=leads, next_after=next_after, has_more=has_more)


def normalize_meta_lead(payload, *, expected_leadgen_id="", expected_form_id=""):
    if not isinstance(payload, dict):
        raise MetaApiError("Meta lead response is invalid")
    leadgen_id = _identifier(payload.get("id"), "lead ID")
    form_id = _identifier(payload.get("form_id"), "form ID")
    if expected_leadgen_id and leadgen_id != expected_leadgen_id:
        raise MetaApiError("Meta lead identity is inconsistent")
    if expected_form_id and form_id != expected_form_id:
        raise MetaApiError("Meta lead form is inconsistent")
    ad_id = _optional_identifier(payload.get("ad_id"), "ad ID")
    campaign_id = _optional_identifier(payload.get("campaign_id"), "campaign ID")
    created_at = _provider_datetime(payload.get("created_time"))
    raw_fields = payload.get("field_data")
    if not isinstance(raw_fields, list) or len(raw_fields) > _MAX_FIELDS:
        raise MetaApiError("Meta lead field data is invalid")
    fields = []
    names = set()
    for item in raw_fields:
        field = _lead_field(item)
        if field.name in names:
            raise MetaApiError("Meta lead field data is inconsistent")
        names.add(field.name)
        fields.append(field)
    canonical = {
        "id": leadgen_id,
        "form_id": form_id,
        "ad_id": ad_id,
        "campaign_id": campaign_id,
        "created_time": created_at.isoformat(),
        "field_data": [
            {"name": field.name, "values": list(field.values)} for field in fields
        ],
    }
    return MetaLead(
        leadgen_id=leadgen_id,
        form_id=form_id,
        ad_id=ad_id,
        campaign_id=campaign_id,
        created_at=created_at,
        fields=tuple(fields),
        payload_sha256=hashlib.sha256(
            canonical_json(canonical).encode("utf-8")
        ).hexdigest(),
    )


def _lead_field(value):
    if not isinstance(value, dict) or set(value) - {"name", "values"}:
        raise MetaApiError("Meta lead field data is invalid")
    name = _bounded_text(value.get("name"), "lead field name", 128).lower()
    raw_values = value.get("values")
    if not isinstance(raw_values, list) or len(raw_values) > _MAX_VALUES_PER_FIELD:
        raise MetaApiError("Meta lead field values are invalid")
    values = tuple(
        _bounded_text(item, "lead field value", _MAX_VALUE_LENGTH, required=False)
        for item in raw_values
    )
    return MetaLeadField(name=name, values=values)


def _next_cursor(payload, current):
    raw_paging = payload.get("paging")
    if raw_paging in (None, False):
        return "", False
    if not isinstance(raw_paging, dict):
        raise MetaApiError("Meta lead pagination is invalid")
    next_url = raw_paging.get("next")
    if not next_url:
        return "", False
    if not isinstance(next_url, str):
        raise MetaApiError("Meta lead pagination is invalid")
    cursors = raw_paging.get("cursors") or {}
    if not isinstance(cursors, dict):
        raise MetaApiError("Meta lead pagination is invalid")
    after = _bounded_text(
        cursors.get("after"), "lead cursor", _MAX_CURSOR_BYTES, required=False
    )
    if not after or after == current:
        raise MetaApiError("Meta lead pagination is invalid")
    return after, True


def _provider_datetime(value):
    value = _bounded_text(value, "lead creation time", 64)
    try:
        return parse_meta_datetime(value)
    except ValueError:
        raise MetaApiError("Meta lead creation time is invalid") from None


def _unix_time(value):
    if not isinstance(value, datetime.datetime):
        raise MetaApiError("Meta lead reconciliation watermark is invalid")
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    result = int(value.astimezone(datetime.timezone.utc).timestamp())
    if result < 0:
        raise MetaApiError("Meta lead reconciliation watermark is invalid")
    return result


def _identifier(value, label):
    value = _bounded_text(value, label, 40)
    if not _ID_RE.fullmatch(value):
        raise MetaApiError("Meta %s is invalid" % label)
    return value


def _optional_identifier(value, label):
    if value in (None, ""):
        return ""
    return _identifier(value, label)


def _bounded_text(value, label, limit, required=True):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise MetaApiError("Meta %s is invalid" % label)
    value = value.strip()
    if required and not value:
        raise MetaApiError("Meta %s is unavailable" % label)
    if len(value) > limit or any(ord(character) < 32 for character in value):
        raise MetaApiError("Meta %s is invalid" % label)
    return value


def _require_supported_graph_version(app):
    require_marketing_graph_version(getattr(app, "graph_version", ""), "lead_ads")

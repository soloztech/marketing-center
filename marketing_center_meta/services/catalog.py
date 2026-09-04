import datetime
import json
import re
from dataclasses import dataclass

from odoo.addons.marketing_center_base.services.catalog_dto import (
    CatalogDTOValidationError,
    ExternalEntityDTO,
    SyncPageDTO,
    canonical_json,
)
from odoo.addons.meta_api_base.services.errors import MetaApiError
from odoo.addons.meta_api_base.services.graph import graph_request

from .datetime_utils import parse_meta_datetime
from .graph_contract import require_marketing_graph_version

META_CATALOG_CONTRACT_VERSION = "meta.marketing.catalog.v1"
META_CATALOG_ENTITY_TYPES = ("campaign", "group", "ad", "creative")
META_CATALOG_RUN_ENTITY_TYPE = "meta_account_catalog"
META_CATALOG_CURSOR_VERSION = 1

_ACCOUNT_REF_RE = re.compile(r"^act_[0-9]+$")
_OBJECT_ID_RE = re.compile(r"^[0-9]+$")
_ENUM_RE = re.compile(r"^[A-Za-z0-9_:-]+$")
_MAX_PAGE_ITEMS = 200
_PAGE_LIMIT = 100
_MAX_CURSOR_BYTES = 3072


@dataclass(frozen=True)
class MetaCatalogSpec:
    entity_type: str
    edge: str
    ref_segment: str
    fields: tuple
    parent_entity_type: str = ""
    parent_field: str = ""
    group_type: str = ""


_CATALOG_SPECS = {
    "campaign": MetaCatalogSpec(
        entity_type="campaign",
        edge="campaigns",
        ref_segment="campaigns",
        fields=(
            "id",
            "name",
            "status",
            "effective_status",
            "objective",
            "buying_type",
            "special_ad_categories",
            "daily_budget",
            "lifetime_budget",
            "start_time",
            "stop_time",
            "created_time",
            "updated_time",
        ),
    ),
    "group": MetaCatalogSpec(
        entity_type="group",
        edge="adsets",
        ref_segment="adsets",
        fields=(
            "id",
            "name",
            "campaign_id",
            "status",
            "effective_status",
            "optimization_goal",
            "billing_event",
            "bid_strategy",
            "bid_amount",
            "daily_budget",
            "lifetime_budget",
            "start_time",
            "end_time",
            "created_time",
            "updated_time",
        ),
        parent_entity_type="campaign",
        parent_field="campaign_id",
        group_type="meta_adset",
    ),
    "ad": MetaCatalogSpec(
        entity_type="ad",
        edge="ads",
        ref_segment="ads",
        fields=(
            "id",
            "name",
            "adset_id",
            "campaign_id",
            "status",
            "effective_status",
            "creative{id}",
            "created_time",
            "updated_time",
        ),
        parent_entity_type="group",
        parent_field="adset_id",
    ),
    "creative": MetaCatalogSpec(
        entity_type="creative",
        edge="adcreatives",
        ref_segment="creatives",
        fields=(
            "id",
            "name",
            "status",
            "object_type",
            "image_hash",
            "video_id",
            "object_story_id",
            "effective_object_story_id",
            "instagram_user_id",
        ),
    ),
}

_ATTRIBUTE_FIELDS = {
    "campaign": (
        "status",
        "effective_status",
        "objective",
        "buying_type",
        "daily_budget",
        "lifetime_budget",
        "start_time",
        "stop_time",
        "created_time",
    ),
    "group": (
        "status",
        "effective_status",
        "optimization_goal",
        "billing_event",
        "bid_strategy",
        "bid_amount",
        "daily_budget",
        "lifetime_budget",
        "start_time",
        "end_time",
        "created_time",
    ),
    "ad": ("status", "effective_status", "created_time"),
    "creative": (
        "status",
        "object_type",
        "image_hash",
        "video_id",
        "object_story_id",
        "effective_object_story_id",
        "instagram_user_id",
    ),
}


def meta_catalog_spec(entity_type):
    spec = _CATALOG_SPECS.get(str(entity_type or "").strip().lower())
    if not spec:
        raise MetaApiError("Meta catalog entity type is unsupported")
    return spec


def meta_catalog_reporting_context(graph_version, account_ref, entity_type):
    account_ref = _account_ref(account_ref)
    spec = meta_catalog_spec(entity_type)
    graph_version = require_marketing_graph_version(graph_version, "catalog")
    return {
        "provider": "meta",
        "graph_version": graph_version,
        "contract_version": META_CATALOG_CONTRACT_VERSION,
        "account_ref": account_ref,
        "entity_type": spec.entity_type,
        "edge": spec.edge,
        "fields": list(spec.fields),
        "filter": "none",
        "page_limit": _PAGE_LIMIT,
    }


def meta_catalog_sweep_reporting_context(graph_version, account_ref):
    account_ref = _account_ref(account_ref)
    graph_version = require_marketing_graph_version(graph_version, "catalog")
    return {
        "provider": "meta",
        "graph_version": graph_version,
        "contract_version": META_CATALOG_CONTRACT_VERSION,
        "cursor_version": META_CATALOG_CURSOR_VERSION,
        "account_ref": account_ref,
        "entity_type": META_CATALOG_RUN_ENTITY_TYPE,
        "stages": [
            {
                "entity_type": spec.entity_type,
                "edge": spec.edge,
                "fields": list(spec.fields),
                "filter": "none",
            }
            for spec in (_CATALOG_SPECS[key] for key in META_CATALOG_ENTITY_TYPES)
        ],
        "page_limit": _PAGE_LIMIT,
    }


def decode_meta_catalog_cursor(value):
    """Return the strict sweep stage/cursor represented by the core cursor."""

    if value in (None, ""):
        return META_CATALOG_ENTITY_TYPES[0], ""
    if not isinstance(value, str) or len(value.encode("utf-8")) > 4096:
        raise MetaApiError("Meta catalog cursor is invalid")
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, RecursionError):
        raise MetaApiError("Meta catalog cursor is invalid") from None
    if not isinstance(payload, dict) or set(payload) != {"version", "stage", "after"}:
        raise MetaApiError("Meta catalog cursor is invalid")
    if payload.get("version") != META_CATALOG_CURSOR_VERSION:
        raise MetaApiError("Meta catalog cursor is invalid")
    stage = payload.get("stage")
    if stage not in META_CATALOG_ENTITY_TYPES:
        raise MetaApiError("Meta catalog cursor is invalid")
    after = _bounded_text(
        payload.get("after"),
        "catalog cursor",
        _MAX_CURSOR_BYTES,
        required=False,
    )
    return stage, after


def orchestrate_meta_catalog_page(stage, page):
    """Wrap an edge page in the single-run, ordered sweep cursor contract."""

    if stage not in META_CATALOG_ENTITY_TYPES or not isinstance(page, SyncPageDTO):
        raise MetaApiError("Meta catalog stage is invalid")
    if page.has_more:
        next_cursor = _encode_meta_catalog_cursor(stage, page.next_cursor)
        has_more = True
    else:
        index = META_CATALOG_ENTITY_TYPES.index(stage)
        has_more = index + 1 < len(META_CATALOG_ENTITY_TYPES)
        next_cursor = (
            _encode_meta_catalog_cursor(META_CATALOG_ENTITY_TYPES[index + 1], "")
            if has_more
            else ""
        )
    return SyncPageDTO(
        items=page.items,
        next_cursor=next_cursor,
        has_more=has_more,
        provider_request_id=page.provider_request_id,
        provider_job_ref=page.provider_job_ref,
        provider_job_state=page.provider_job_state,
        watermark=page.watermark,
        reporting_context_hash=page.reporting_context_hash,
        retry_after=page.retry_after,
        errors=page.errors,
        # Absence reconciliation is intentionally outside this release.
        authoritative_complete=False,
    )


def fetch_meta_catalog_page(
    app,
    access_token,
    account_ref,
    entity_type,
    *,
    after="",
    reporting_context_hash,
    observed_at=None,
):
    """Fetch and normalize exactly one bounded Meta catalog page."""

    require_marketing_graph_version(app.graph_version, "catalog")
    account_ref = _account_ref(account_ref)
    spec = meta_catalog_spec(entity_type)
    after = _bounded_text(
        after,
        "catalog cursor",
        _MAX_CURSOR_BYTES,
        required=False,
    )
    params = {"fields": ",".join(spec.fields), "limit": _PAGE_LIMIT}
    if after:
        params["after"] = after
    payload = graph_request(
        app,
        access_token,
        "GET",
        "%s/%s" % (account_ref, spec.edge),
        params=params,
        max_response_bytes=1024 * 1024,
    )
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > _MAX_PAGE_ITEMS:
        raise MetaApiError("Meta catalog page is invalid")
    observed_at = _observed_at(observed_at)
    try:
        items = tuple(
            _external_entity(account_ref, spec, row, observed_at) for row in rows
        )
        next_cursor, has_more = _next_cursor(payload, after)
        return SyncPageDTO(
            items=items,
            next_cursor=next_cursor,
            has_more=has_more,
            reporting_context_hash=reporting_context_hash,
            # A terminal Graph page alone is not sufficient evidence for deletion.
            authoritative_complete=False,
        )
    except CatalogDTOValidationError:
        raise MetaApiError("Meta catalog page normalization failed") from None


def _external_entity(account_ref, spec, row, observed_at):
    if not isinstance(row, dict):
        raise MetaApiError("Meta catalog row is invalid")
    external_id = _object_id(row.get("id"), "object ID")
    parent_external_ref = ""
    if spec.parent_field:
        parent_id = _object_id(row.get(spec.parent_field), "parent object ID")
        parent_segment = {
            "campaign": "campaigns",
            "group": "adsets",
        }[spec.parent_entity_type]
        parent_external_ref = _entity_ref(account_ref, parent_segment, parent_id)
    name = _bounded_text(row.get("name"), "object name", 1024, required=False)
    if not name:
        name = "Meta %s %s" % (spec.entity_type, external_id)
    attributes = _attributes(account_ref, spec, row)
    effective_status = _optional_enum(row.get("effective_status"), "effective status")
    configured_status = _optional_enum(row.get("status"), "configured status")
    return ExternalEntityDTO(
        entity_type=spec.entity_type,
        external_ref=_entity_ref(account_ref, spec.ref_segment, external_id),
        external_id=external_id,
        name=name,
        remote_status=effective_status or configured_status,
        group_type=spec.group_type,
        parent_entity_type=spec.parent_entity_type,
        parent_external_ref=parent_external_ref,
        observed_at=observed_at,
        provider_updated_at=_optional_datetime(row.get("updated_time")),
        attributes=attributes,
        source_schema_version=META_CATALOG_CONTRACT_VERSION,
    )


def _attributes(account_ref, spec, row):
    values = {}
    for field_name in _ATTRIBUTE_FIELDS[spec.entity_type]:
        value = row.get(field_name)
        if value in (None, ""):
            continue
        if field_name in {
            "status",
            "effective_status",
            "objective",
            "buying_type",
            "optimization_goal",
            "billing_event",
            "bid_strategy",
            "object_type",
        }:
            value = _optional_enum(value, field_name)
        elif field_name.endswith("_time"):
            value = _wire_datetime(_optional_datetime(value), field_name)
        elif field_name in {
            "bid_amount",
            "daily_budget",
            "lifetime_budget",
        }:
            value = _minor_units(value, field_name)
        else:
            value = _bounded_text(value, field_name, 256)
        values["meta.%s" % field_name] = value
    categories = row.get("special_ad_categories")
    if categories not in (None, False, []):
        if not isinstance(categories, list) or len(categories) > 64:
            raise MetaApiError("Meta special ad categories are invalid")
        values["meta.special_ad_categories"] = sorted(
            {
                _optional_enum(value, "special ad category", required=True)
                for value in categories
            }
        )
    if spec.entity_type == "ad":
        campaign_id = row.get("campaign_id")
        if campaign_id not in (None, ""):
            values["meta.campaign_ref"] = _entity_ref(
                account_ref,
                "campaigns",
                _object_id(campaign_id, "campaign ID"),
            )
        creative = row.get("creative")
        if creative not in (None, False):
            if not isinstance(creative, dict):
                raise MetaApiError("Meta ad creative reference is invalid")
            creative_id = _object_id(creative.get("id"), "creative ID")
            values["meta.creative_ref"] = _entity_ref(
                account_ref,
                "creatives",
                creative_id,
            )
    return values


def _next_cursor(payload, current_cursor):
    raw_paging = payload.get("paging")
    if (
        raw_paging is not None
        and raw_paging is not False
        and not isinstance(raw_paging, dict)
    ):
        raise MetaApiError("Meta catalog pagination is invalid")
    paging = raw_paging or {}
    raw_cursors = paging.get("cursors")
    if (
        raw_cursors is not None
        and raw_cursors is not False
        and not isinstance(raw_cursors, dict)
    ):
        raise MetaApiError("Meta catalog pagination is invalid")
    next_page = paging.get("next")
    if next_page is None or next_page is False or next_page == "":
        return "", False
    if not isinstance(next_page, str):
        raise MetaApiError("Meta catalog pagination is invalid")
    next_cursor = _bounded_text(
        (raw_cursors or {}).get("after"),
        "catalog cursor",
        _MAX_CURSOR_BYTES,
        required=False,
    )
    if not next_cursor or next_cursor == current_cursor:
        raise MetaApiError("Meta catalog pagination is invalid")
    return next_cursor, True


def _encode_meta_catalog_cursor(stage, after):
    if stage not in META_CATALOG_ENTITY_TYPES:
        raise MetaApiError("Meta catalog stage is invalid")
    after = _bounded_text(
        after,
        "catalog cursor",
        _MAX_CURSOR_BYTES,
        required=False,
    )
    value = canonical_json(
        {
            "version": META_CATALOG_CURSOR_VERSION,
            "stage": stage,
            "after": after,
        }
    )
    if len(value.encode("utf-8")) > 4096:
        raise MetaApiError("Meta catalog cursor is invalid")
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


def _entity_ref(account_ref, segment, external_id):
    return "%s/%s/%s" % (account_ref, segment, external_id)


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


def _optional_enum(value, label, required=False):
    value = _bounded_text(value, label, 128, required=required)
    if value and not _ENUM_RE.fullmatch(value):
        raise MetaApiError("Meta %s is invalid" % label)
    return value.lower()


def _minor_units(value, label):
    if isinstance(value, bool):
        raise MetaApiError("Meta %s is invalid" % label)
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isdigit():
        try:
            result = int(value)
        except ValueError:
            raise MetaApiError("Meta %s is invalid" % label) from None
    else:
        raise MetaApiError("Meta %s is invalid" % label)
    if result < 0 or result > 2**63 - 1:
        raise MetaApiError("Meta %s is invalid" % label)
    return result


def _optional_datetime(value):
    if value in (None, ""):
        return None
    value = _bounded_text(value, "object timestamp", 64)
    try:
        return parse_meta_datetime(value)
    except ValueError:
        raise MetaApiError("Meta object timestamp is invalid") from None


def _wire_datetime(value, label):
    if not value:
        raise MetaApiError("Meta %s is invalid" % label)
    return "%sZ" % value.isoformat()


def _observed_at(value):
    if value is None:
        value = datetime.datetime.utcnow()
    if not isinstance(value, datetime.datetime):
        raise MetaApiError("Meta catalog observation time is invalid")
    if value.tzinfo:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)

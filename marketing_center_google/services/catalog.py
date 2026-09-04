import dataclasses
import datetime
import json
import re

from odoo.addons.google_api_base.services.errors import GoogleApiError
from odoo.addons.marketing_center_base.services.catalog_dto import (
    ExternalEntityDTO,
    SyncPageDTO,
)

GOOGLE_ADS_API_VERSION = "v25"
GOOGLE_ADS_SERVICE = "google.ads"
GOOGLE_ADAPTER_KEY = "google.ads.rest.v25"
GOOGLE_CATALOG_CONTRACT_VERSION = "google.ads.catalog.v25.2"
GOOGLE_CATALOG_RUN_ENTITY_TYPE = "google_ads_catalog"
GOOGLE_CATALOG_ENTITY_TYPES = (
    "campaign",
    "ad_group",
    "ad",
    "asset",
    "keyword",
    "conversion_action",
)

_CUSTOMER_ID_RE = re.compile(r"^[0-9]{10}$")
_RESOURCE_RE = re.compile(r"^customers/[0-9]{10}(?:/[A-Za-z]+/[A-Za-z0-9~_-]+)?$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_STATUS_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_MAX_CURSOR_BYTES = 4096
_MAX_PROVIDER_TOKEN_BYTES = 3072
_CATALOG_CHUNK_SIZE = 2000


@dataclasses.dataclass(frozen=True)
class GoogleCatalogSpec:
    entity_type: str
    query: str
    row_key: str
    resource_key: str
    id_key: str
    name_key: str
    status_key: str
    parent_entity_type: str = ""
    parent_row_key: str = ""
    attribute_keys: tuple = ()


def _query(value):
    return " ".join(value.split())


_CATALOG_SPECS = {
    "campaign": GoogleCatalogSpec(
        entity_type="campaign",
        query=_query(
            """
            SELECT campaign.resource_name, campaign.id, campaign.name,
                   campaign.status, campaign.advertising_channel_type,
                   campaign.start_date_time, campaign.end_date_time
              FROM campaign
             ORDER BY campaign.id
            """
        ),
        row_key="campaign",
        resource_key="resourceName",
        id_key="id",
        name_key="name",
        status_key="status",
        attribute_keys=(
            "advertisingChannelType",
            "startDateTime",
            "endDateTime",
        ),
    ),
    "ad_group": GoogleCatalogSpec(
        entity_type="ad_group",
        query=_query(
            """
            SELECT campaign.resource_name, campaign.id,
                   ad_group.resource_name, ad_group.id, ad_group.name,
                   ad_group.status, ad_group.type
              FROM ad_group
             ORDER BY campaign.id, ad_group.id
            """
        ),
        row_key="adGroup",
        resource_key="resourceName",
        id_key="id",
        name_key="name",
        status_key="status",
        parent_entity_type="campaign",
        parent_row_key="campaign",
        attribute_keys=("type",),
    ),
    "ad": GoogleCatalogSpec(
        entity_type="ad",
        query=_query(
            """
            SELECT ad_group.resource_name, ad_group.id,
                   ad_group_ad.resource_name, ad_group_ad.ad.id,
                   ad_group_ad.ad.name,
                   ad_group_ad.ad.type, ad_group_ad.ad.final_urls,
                   ad_group_ad.status
              FROM ad_group_ad
             ORDER BY ad_group.id, ad_group_ad.ad.id
            """
        ),
        row_key="adGroupAd",
        resource_key="resourceName",
        id_key="ad.id",
        name_key="ad.name",
        status_key="status",
        parent_entity_type="ad_group",
        parent_row_key="adGroup",
        attribute_keys=("ad.type", "ad.finalUrls"),
    ),
    "asset": GoogleCatalogSpec(
        entity_type="asset",
        query=_query(
            """
            SELECT asset.resource_name, asset.id, asset.name,
                   asset.type, asset.source
              FROM asset
             ORDER BY asset.id
            """
        ),
        row_key="asset",
        resource_key="resourceName",
        id_key="id",
        name_key="name",
        status_key="",
        attribute_keys=("type", "source"),
    ),
    "keyword": GoogleCatalogSpec(
        entity_type="keyword",
        query=_query(
            """
            SELECT ad_group.resource_name, ad_group.id,
                   ad_group_criterion.resource_name,
                   ad_group_criterion.criterion_id,
                   ad_group_criterion.status, ad_group_criterion.negative,
                   ad_group_criterion.keyword.text,
                   ad_group_criterion.keyword.match_type
              FROM keyword_view
             ORDER BY ad_group.id, ad_group_criterion.criterion_id
            """
        ),
        row_key="adGroupCriterion",
        resource_key="resourceName",
        id_key="criterionId",
        name_key="keyword.text",
        status_key="status",
        parent_entity_type="ad_group",
        parent_row_key="adGroup",
        attribute_keys=("negative", "keyword.matchType"),
    ),
    "conversion_action": GoogleCatalogSpec(
        entity_type="conversion_action",
        query=_query(
            """
            SELECT conversion_action.resource_name, conversion_action.id,
                   conversion_action.name, conversion_action.status,
                   conversion_action.type, conversion_action.category,
                   conversion_action.primary_for_goal,
                   conversion_action.include_in_conversions_metric
              FROM conversion_action
             ORDER BY conversion_action.id
            """
        ),
        row_key="conversionAction",
        resource_key="resourceName",
        id_key="id",
        name_key="name",
        status_key="status",
        attribute_keys=(
            "type",
            "category",
            "primaryForGoal",
            "includeInConversionsMetric",
        ),
    ),
}


def google_catalog_spec(entity_type):
    spec = _CATALOG_SPECS.get(_safe_stage(entity_type))
    if not spec:
        raise GoogleApiError("Google Ads catalog stage is unsupported")
    return spec


def google_catalog_reporting_context(customer_id):
    customer_id = normalize_customer_id(customer_id)
    return {
        "provider": "google_ads",
        "api_version": GOOGLE_ADS_API_VERSION,
        "contract_version": GOOGLE_CATALOG_CONTRACT_VERSION,
        "customer_ref": "customers/%s" % customer_id,
        "entity_types": list(GOOGLE_CATALOG_ENTITY_TYPES),
        "query_hash_policy": "fixed_allowlist",
        "absence_policy": "disabled",
    }


def encode_google_catalog_cursor(stage, page_token=""):
    stage = _safe_stage(stage)
    page_token = _page_token(page_token)
    value = json.dumps(
        {"stage": stage, "token": page_token, "version": 1},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(value.encode("utf-8")) > _MAX_CURSOR_BYTES:
        raise GoogleApiError("Google Ads catalog cursor is invalid")
    return value


def decode_google_catalog_cursor(value):
    if not value:
        return GOOGLE_CATALOG_ENTITY_TYPES[0], ""
    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_CURSOR_BYTES:
        raise GoogleApiError("Google Ads catalog cursor is invalid")
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        raise GoogleApiError("Google Ads catalog cursor is invalid") from None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"stage", "token", "version"}
        or payload.get("version") != 1
    ):
        raise GoogleApiError("Google Ads catalog cursor is invalid")
    return _safe_stage(payload.get("stage")), _page_token(payload.get("token"))


def normalize_google_catalog_page(
    customer_id,
    stage,
    page,
    *,
    current_page_token="",
    reporting_context_hash,
    observed_at=None,
):
    """Normalize one fixed 10k Google page into bounded core DTO chunks."""

    customer_id = normalize_customer_id(customer_id)
    spec = google_catalog_spec(stage)
    current_page_token = _page_token(current_page_token)
    rows = getattr(page, "results", None)
    if not isinstance(rows, tuple) or len(rows) > 10_000:
        raise GoogleApiError("Google Ads catalog page is invalid")
    observed_at = _observed_at(observed_at)
    items = tuple(_external_entity(customer_id, spec, row, observed_at) for row in rows)
    next_page_token = _page_token(getattr(page, "next_page_token", ""))
    if next_page_token and next_page_token == current_page_token:
        raise GoogleApiError("Google Ads catalog pagination made no progress")
    final_cursor, final_has_more = _next_catalog_cursor(
        spec.entity_type, next_page_token
    )
    return _catalog_chunks(
        items,
        stage=spec.entity_type,
        current_page_token=current_page_token,
        final_cursor=final_cursor,
        final_has_more=final_has_more,
        request_id=getattr(page, "request_id", ""),
        reporting_context_hash=reporting_context_hash,
    )


def normalize_customer_id(value):
    value = str(value or "").strip().replace("-", "")
    if not _CUSTOMER_ID_RE.fullmatch(value):
        raise GoogleApiError("Google Ads customer ID is invalid")
    return value


def _external_entity(customer_id, spec, row, observed_at):
    if not isinstance(row, dict):
        raise GoogleApiError("Google Ads catalog row is invalid")
    values = _mapping(row.get(spec.row_key), spec.row_key)
    external_ref = _resource(_nested(values, spec.resource_key), customer_id)
    external_id = _identifier(_nested(values, spec.id_key), "entity ID")
    name = _optional_text(_nested(values, spec.name_key), "entity name", 1024)
    if not name:
        name = "Google Ads %s %s" % (spec.entity_type.replace("_", " "), external_id)
    status = ""
    if spec.status_key:
        status = _enum(_nested(values, spec.status_key), "entity status")
    parent_ref = ""
    if spec.parent_row_key:
        parent = _mapping(row.get(spec.parent_row_key), spec.parent_row_key)
        parent_ref = _resource(parent.get("resourceName"), customer_id)
    attributes = {}
    for path in spec.attribute_keys:
        value = _nested(values, path)
        if value in (None, "", []):
            continue
        attributes["google.%s" % path] = _attribute(value, path)
    return ExternalEntityDTO(
        entity_type=spec.entity_type,
        external_ref=external_ref,
        external_id=external_id,
        name=name,
        remote_status=status,
        parent_entity_type=spec.parent_entity_type,
        parent_external_ref=parent_ref,
        observed_at=observed_at,
        attributes=attributes,
        source_schema_version=GOOGLE_CATALOG_CONTRACT_VERSION,
    )


def _catalog_chunks(
    items,
    *,
    stage,
    current_page_token,
    final_cursor,
    final_has_more,
    request_id,
    reporting_context_hash,
):
    chunks = [
        items[index : index + _CATALOG_CHUNK_SIZE]
        for index in range(0, len(items), _CATALOG_CHUNK_SIZE)
    ] or [()]
    retry_cursor = encode_google_catalog_cursor(stage, current_page_token)
    pages = []
    for index, chunk in enumerate(chunks):
        terminal_chunk = index == len(chunks) - 1
        pages.append(
            SyncPageDTO(
                items=tuple(chunk),
                next_cursor=final_cursor if terminal_chunk else retry_cursor,
                has_more=final_has_more if terminal_chunk else True,
                provider_request_id=request_id,
                reporting_context_hash=reporting_context_hash,
                authoritative_complete=False,
            )
        )
    return tuple(pages)


def _next_catalog_cursor(stage, next_page_token):
    next_page_token = _page_token(next_page_token)
    if next_page_token:
        return encode_google_catalog_cursor(stage, next_page_token), True
    index = GOOGLE_CATALOG_ENTITY_TYPES.index(stage)
    if index + 1 >= len(GOOGLE_CATALOG_ENTITY_TYPES):
        return "", False
    return encode_google_catalog_cursor(GOOGLE_CATALOG_ENTITY_TYPES[index + 1]), True


def _safe_stage(value):
    value = str(value or "").strip().lower()
    if value not in GOOGLE_CATALOG_ENTITY_TYPES:
        raise GoogleApiError("Google Ads catalog stage is invalid")
    return value


def _page_token(value):
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise GoogleApiError("Google Ads catalog page token is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise GoogleApiError("Google Ads catalog page token is invalid") from None
    if (
        not value
        or len(encoded) > _MAX_PROVIDER_TOKEN_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise GoogleApiError("Google Ads catalog page token is invalid")
    return value


def _mapping(value, label):
    if not isinstance(value, dict):
        raise GoogleApiError("Google Ads %s is invalid" % label)
    return value


def _nested(values, path):
    current = values
    for key in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _resource(value, customer_id):
    value = _text(value, "resource name", 512)
    if not _RESOURCE_RE.fullmatch(value) or not value.startswith(
        "customers/%s" % customer_id
    ):
        raise GoogleApiError("Google Ads resource name is invalid")
    return value


def _identifier(value, label):
    value = _text(value, label, 128)
    if not re.fullmatch(r"[0-9]+", value):
        raise GoogleApiError("Google Ads %s is invalid" % label)
    return value


def _enum(value, label):
    value = _text(value, label, 128).upper()
    if not _STATUS_RE.fullmatch(value):
        raise GoogleApiError("Google Ads %s is invalid" % label)
    return value.lower()


def _attribute(value, label):
    if isinstance(value, bool):
        return value
    if isinstance(value, list):
        if len(value) > 64:
            raise GoogleApiError("Google Ads %s is invalid" % label)
        return [_text(item, label, 2048) for item in value]
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return _text(value, label, 2048)
    raise GoogleApiError("Google Ads %s is invalid" % label)


def _optional_text(value, label, limit):
    if value in (None, ""):
        return ""
    return _text(value, label, limit)


def _text(value, label, limit):
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        raise GoogleApiError("Google Ads %s is invalid" % label)
    value = value.strip()
    if (
        not value
        or len(value) > limit
        or any(ord(character) < 32 for character in value)
    ):
        raise GoogleApiError("Google Ads %s is invalid" % label)
    return value


def _observed_at(value):
    value = value or datetime.datetime.utcnow()
    if not isinstance(value, datetime.datetime):
        raise GoogleApiError("Google Ads observation timestamp is invalid")
    if value.tzinfo:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)

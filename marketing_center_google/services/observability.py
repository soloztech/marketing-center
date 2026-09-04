import dataclasses
import datetime
import hashlib
import json
import re

import pytz

from odoo.addons.google_api_base.services.errors import GoogleApiError
from odoo.addons.marketing_center_base.services.catalog_dto import (
    canonical_json,
    sha256_text,
)
from odoo.addons.marketing_center_base.services.timezone import (
    LocalDateBoundaryError,
    local_date_boundary_utc,
)

from .catalog import GOOGLE_ADS_API_VERSION, normalize_customer_id

GOOGLE_CHANGE_CONTRACT_VERSION = "google.ads.change-history.v25.1"
GOOGLE_DIAGNOSTIC_CONTRACT_VERSION = "google.ads.delivery-diagnostics.v25.1"
GOOGLE_CHANGE_RUN_ENTITY_TYPE = "google_ads_change_history"
GOOGLE_DIAGNOSTIC_RUN_ENTITY_TYPE = "google_ads_delivery_diagnostics"
GOOGLE_DIAGNOSTIC_STAGES = ("campaign", "ad_group", "ad")
GOOGLE_CHANGE_ROW_LIMIT = 10_000

_CHANGE_CHUNK_SIZE = 500
_DIAGNOSTIC_CHUNK_SIZE = 500
_MAX_PROVIDER_TOKEN_BYTES = 3072
_MAX_CURSOR_BYTES = 4096
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ENUM_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_FIELD_PATH_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,255}$")


@dataclasses.dataclass(frozen=True)
class GoogleChangeObservationDTO:
    event_ref: str
    occurred_at: datetime.datetime
    resource_type: str
    resource_ref: str
    operation: str
    client_type: str
    changed_fields: tuple
    actor_hash: str = ""
    observed_at: datetime.datetime = dataclasses.field(
        default_factory=lambda: datetime.datetime.utcnow().replace(microsecond=0)
    )

    def __post_init__(self):
        object.__setattr__(self, "event_ref", _text(self.event_ref, "event ref", 512))
        object.__setattr__(
            self, "occurred_at", _utc_datetime(self.occurred_at, "change timestamp")
        )
        object.__setattr__(
            self, "resource_type", _enum(self.resource_type, "resource type")
        )
        object.__setattr__(
            self, "resource_ref", _text(self.resource_ref, "resource ref", 512)
        )
        object.__setattr__(self, "operation", _enum(self.operation, "operation"))
        object.__setattr__(self, "client_type", _enum(self.client_type, "client type"))
        object.__setattr__(
            self,
            "changed_fields",
            _field_paths(self.changed_fields),
        )
        actor_hash = str(self.actor_hash or "").strip().lower()
        if actor_hash and not _SHA256_RE.fullmatch(actor_hash):
            raise GoogleApiError("Google Ads change actor hash is invalid")
        object.__setattr__(self, "actor_hash", actor_hash)
        object.__setattr__(
            self,
            "observed_at",
            _utc_datetime(self.observed_at, "observation timestamp"),
        )

    @property
    def content_hash(self):
        return sha256_text(
            canonical_json(
                {
                    "actor_hash": self.actor_hash,
                    "changed_fields": list(self.changed_fields),
                    "client_type": self.client_type,
                    "event_ref": self.event_ref,
                    "occurred_at": self.occurred_at.isoformat(),
                    "operation": self.operation,
                    "resource_ref": self.resource_ref,
                    "resource_type": self.resource_type,
                    "schema": GOOGLE_CHANGE_CONTRACT_VERSION,
                }
            )
        )


@dataclasses.dataclass(frozen=True)
class GoogleDiagnosticObservationDTO:
    asset_type: str
    asset_ref: str
    configured_status: str
    primary_status: str
    status_reasons: tuple
    policy_approval_status: str = ""
    policy_review_status: str = ""
    policy_topics: tuple = ()
    observed_at: datetime.datetime = dataclasses.field(
        default_factory=lambda: datetime.datetime.utcnow().replace(microsecond=0)
    )

    def __post_init__(self):
        asset_type = str(self.asset_type or "").strip().lower()
        if asset_type not in GOOGLE_DIAGNOSTIC_STAGES:
            raise GoogleApiError("Google Ads diagnostic asset type is invalid")
        object.__setattr__(self, "asset_type", asset_type)
        object.__setattr__(self, "asset_ref", _text(self.asset_ref, "asset ref", 512))
        object.__setattr__(
            self,
            "configured_status",
            _enum(self.configured_status, "configured status"),
        )
        object.__setattr__(
            self, "primary_status", _enum(self.primary_status, "primary status")
        )
        object.__setattr__(
            self, "status_reasons", _enum_values(self.status_reasons, "status reasons")
        )
        object.__setattr__(
            self,
            "policy_approval_status",
            _optional_enum(self.policy_approval_status, "policy approval status"),
        )
        object.__setattr__(
            self,
            "policy_review_status",
            _optional_enum(self.policy_review_status, "policy review status"),
        )
        object.__setattr__(self, "policy_topics", _policy_topics(self.policy_topics))
        object.__setattr__(
            self,
            "observed_at",
            _utc_datetime(self.observed_at, "observation timestamp"),
        )

    @property
    def severity(self):
        if self.primary_status == "not_eligible" or self.policy_approval_status in {
            "disapproved",
            "area_of_interest_only",
        }:
            return "error"
        if (
            self.primary_status in {"limited", "pending"}
            or self.policy_review_status in {"review_in_progress", "under_appeal"}
            or self.policy_topics
        ):
            return "warning"
        if self.primary_status in {"paused", "removed"}:
            return "info"
        return "ok"

    @property
    def content_hash(self):
        return sha256_text(
            canonical_json(
                {
                    "asset_ref": self.asset_ref,
                    "asset_type": self.asset_type,
                    "configured_status": self.configured_status,
                    "policy_approval_status": self.policy_approval_status,
                    "policy_review_status": self.policy_review_status,
                    "policy_topics": list(self.policy_topics),
                    "primary_status": self.primary_status,
                    "schema": GOOGLE_DIAGNOSTIC_CONTRACT_VERSION,
                    "status_reasons": list(self.status_reasons),
                }
            )
        )


@dataclasses.dataclass(frozen=True)
class GoogleObservationPage:
    observation_kind: str
    items: tuple = ()
    next_cursor: str = ""
    has_more: bool = False
    provider_request_id: str = ""
    reporting_context_hash: str = ""
    watermark: str = ""
    errors: tuple = ()

    def __post_init__(self):
        kind = str(self.observation_kind or "").strip().lower()
        expected = {
            "change": GoogleChangeObservationDTO,
            "diagnostic": GoogleDiagnosticObservationDTO,
        }.get(kind)
        if (
            not expected
            or not isinstance(self.items, tuple)
            or any(not isinstance(item, expected) for item in self.items)
        ):
            raise GoogleApiError("Google Ads observation page is invalid")
        if not isinstance(self.has_more, bool) or self.errors:
            raise GoogleApiError("Google Ads observation page is invalid")
        object.__setattr__(self, "observation_kind", kind)
        object.__setattr__(self, "next_cursor", _page_token(self.next_cursor))
        object.__setattr__(
            self,
            "provider_request_id",
            _optional_text(self.provider_request_id, "request ID", 128),
        )
        context_hash = str(self.reporting_context_hash or "").strip().lower()
        if not _SHA256_RE.fullmatch(context_hash):
            raise GoogleApiError("Google Ads reporting context hash is invalid")
        object.__setattr__(self, "reporting_context_hash", context_hash)
        object.__setattr__(
            self, "watermark", _optional_text(self.watermark, "watermark", 128)
        )


@dataclasses.dataclass(frozen=True)
class GoogleObservationIngestResult:
    disposition: str
    content_hash: str
    record_id: int


@dataclasses.dataclass(frozen=True)
class GoogleDiagnosticSpec:
    asset_type: str
    row_key: str
    query: str


def google_change_query(local_date):
    local_date = _date(local_date, "change date")
    next_date = local_date + datetime.timedelta(days=1)
    return " ".join(
        (
            "SELECT change_event.resource_name,",
            "change_event.change_date_time,",
            "change_event.change_resource_name,",
            "change_event.user_email, change_event.client_type,",
            "change_event.change_resource_type,",
            "change_event.resource_change_operation,",
            "change_event.changed_fields",
            "FROM change_event",
            "WHERE change_event.change_date_time >= '%s 00:00:00'"
            % local_date.isoformat(),
            "AND change_event.change_date_time < '%s 00:00:00'" % next_date.isoformat(),
            "ORDER BY change_event.change_date_time",
            "LIMIT %s" % GOOGLE_CHANGE_ROW_LIMIT,
        )
    )


def google_change_reporting_context(customer_id, local_date, report_timezone):
    customer_id = normalize_customer_id(customer_id)
    local_date = _date(local_date, "change date")
    _timezone(report_timezone)
    return {
        "provider": "google_ads",
        "api_version": GOOGLE_ADS_API_VERSION,
        "contract_version": GOOGLE_CHANGE_CONTRACT_VERSION,
        "customer_ref": "customers/%s" % customer_id,
        "local_date": local_date.isoformat(),
        "report_timezone": report_timezone,
        "limit": GOOGLE_CHANGE_ROW_LIMIT,
        "payload_policy": "metadata_allowlist_no_old_new_resource",
    }


def google_diagnostic_spec(stage):
    stage = str(stage or "").strip().lower()
    spec = _DIAGNOSTIC_SPECS.get(stage)
    if not spec:
        raise GoogleApiError("Google Ads diagnostic stage is invalid")
    return spec


def google_diagnostic_reporting_context(customer_id):
    customer_id = normalize_customer_id(customer_id)
    return {
        "provider": "google_ads",
        "api_version": GOOGLE_ADS_API_VERSION,
        "contract_version": GOOGLE_DIAGNOSTIC_CONTRACT_VERSION,
        "customer_ref": "customers/%s" % customer_id,
        "asset_types": list(GOOGLE_DIAGNOSTIC_STAGES),
        "payload_policy": "delivery_status_allowlist",
    }


def encode_google_diagnostic_cursor(stage, page_token=""):
    stage = google_diagnostic_spec(stage).asset_type
    page_token = _page_token(page_token)
    value = json.dumps(
        {"stage": stage, "token": page_token, "version": 1},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(value.encode("utf-8")) > _MAX_CURSOR_BYTES:
        raise GoogleApiError("Google Ads diagnostic cursor is invalid")
    return value


def decode_google_diagnostic_cursor(value):
    if not value:
        return GOOGLE_DIAGNOSTIC_STAGES[0], ""
    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_CURSOR_BYTES:
        raise GoogleApiError("Google Ads diagnostic cursor is invalid")
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        raise GoogleApiError("Google Ads diagnostic cursor is invalid") from None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"stage", "token", "version"}
        or payload.get("version") != 1
    ):
        raise GoogleApiError("Google Ads diagnostic cursor is invalid")
    return (
        google_diagnostic_spec(payload.get("stage")).asset_type,
        _page_token(payload.get("token")),
    )


def normalize_observation_window(local_date, report_timezone):
    local_date = _date(local_date, "observation date")
    report_timezone = _timezone(report_timezone)
    try:
        start = local_date_boundary_utc(local_date, report_timezone)
        end = local_date_boundary_utc(
            local_date + datetime.timedelta(days=1), report_timezone
        )
    except (LocalDateBoundaryError, OverflowError):
        raise GoogleApiError("Google Ads observation window is invalid") from None
    return start, end


def observation_date_from_utc(window_start, window_end, report_timezone):
    report_timezone = _timezone(report_timezone)
    if (
        not isinstance(window_start, datetime.datetime)
        or not isinstance(window_end, datetime.datetime)
        or window_start.tzinfo
        or window_end.tzinfo
    ):
        raise GoogleApiError("Google Ads observation window is invalid")
    zone = pytz.timezone(report_timezone)
    local_date = pytz.UTC.localize(window_start).astimezone(zone).date()
    expected = normalize_observation_window(local_date, report_timezone)
    if expected != (window_start, window_end):
        raise GoogleApiError("Google Ads observation window is invalid")
    return local_date


def normalize_google_change_page(
    customer_id,
    page,
    *,
    current_page_token="",
    local_date,
    report_timezone,
    reporting_context_hash,
    observed_at=None,
):
    customer_id = normalize_customer_id(customer_id)
    current_page_token = _page_token(current_page_token)
    local_date = _date(local_date, "change date")
    report_timezone = _timezone(report_timezone)
    rows = getattr(page, "results", None)
    if not isinstance(rows, tuple) or len(rows) > GOOGLE_CHANGE_ROW_LIMIT:
        raise GoogleApiError("Google Ads change page is invalid")
    observed_at = _utc_datetime(
        observed_at or datetime.datetime.utcnow(), "observation timestamp"
    )
    items = tuple(
        _change_row(
            row,
            customer_id=customer_id,
            local_date=local_date,
            report_timezone=report_timezone,
            observed_at=observed_at,
        )
        for row in rows
    )
    next_token = _page_token(getattr(page, "next_page_token", ""))
    if next_token and next_token == current_page_token:
        raise GoogleApiError("Google Ads change pagination made no progress")
    return _observation_chunks(
        "change",
        items,
        current_cursor=current_page_token,
        next_cursor=next_token,
        final_has_more=bool(next_token),
        request_id=getattr(page, "request_id", ""),
        reporting_context_hash=reporting_context_hash,
        watermark=local_date.isoformat(),
        chunk_size=_CHANGE_CHUNK_SIZE,
    )


def normalize_google_diagnostic_page(
    customer_id,
    stage,
    page,
    *,
    current_page_token="",
    reporting_context_hash,
    observed_at=None,
):
    customer_id = normalize_customer_id(customer_id)
    spec = google_diagnostic_spec(stage)
    current_page_token = _page_token(current_page_token)
    rows = getattr(page, "results", None)
    if not isinstance(rows, tuple) or len(rows) > GOOGLE_CHANGE_ROW_LIMIT:
        raise GoogleApiError("Google Ads diagnostic page is invalid")
    observed_at = _utc_datetime(
        observed_at or datetime.datetime.utcnow(), "observation timestamp"
    )
    items = tuple(
        _diagnostic_row(
            row,
            customer_id=customer_id,
            spec=spec,
            observed_at=observed_at,
        )
        for row in rows
    )
    next_token = _page_token(getattr(page, "next_page_token", ""))
    if next_token and next_token == current_page_token:
        raise GoogleApiError("Google Ads diagnostic pagination made no progress")
    final_cursor, final_has_more = _next_diagnostic_cursor(spec.asset_type, next_token)
    return _observation_chunks(
        "diagnostic",
        items,
        current_cursor=encode_google_diagnostic_cursor(
            spec.asset_type, current_page_token
        ),
        next_cursor=final_cursor,
        final_has_more=final_has_more,
        request_id=getattr(page, "request_id", ""),
        reporting_context_hash=reporting_context_hash,
        watermark=spec.asset_type,
        chunk_size=_DIAGNOSTIC_CHUNK_SIZE,
    )


def _change_row(row, *, customer_id, local_date, report_timezone, observed_at):
    values = _mapping(row, "change row").get("changeEvent")
    values = _mapping(values, "change event")
    event_ref = _customer_resource(
        values.get("resourceName"), customer_id, "changeEvents"
    )
    occurred_at = _provider_local_datetime(
        values.get("changeDateTime"), report_timezone
    )
    zone = pytz.timezone(report_timezone)
    if pytz.UTC.localize(occurred_at).astimezone(zone).date() != local_date:
        raise GoogleApiError("Google Ads change event is outside its window")
    resource_ref = _text(values.get("changeResourceName"), "resource ref", 512)
    if not resource_ref.startswith("customers/%s/" % customer_id):
        raise GoogleApiError("Google Ads changed resource is invalid")
    actor_hash = _actor_hash(values.get("userEmail"))
    return GoogleChangeObservationDTO(
        event_ref=event_ref,
        occurred_at=occurred_at,
        resource_type=values.get("changeResourceType"),
        resource_ref=resource_ref,
        operation=values.get("resourceChangeOperation"),
        client_type=values.get("clientType"),
        changed_fields=_changed_fields(values.get("changedFields")),
        actor_hash=actor_hash,
        observed_at=observed_at,
    )


def _diagnostic_row(row, *, customer_id, spec, observed_at):
    values = _mapping(row, "diagnostic row").get(spec.row_key)
    values = _mapping(values, "diagnostic resource")
    asset_ref = _customer_resource(
        values.get("resourceName"), customer_id, _RESOURCE_SEGMENT[spec.asset_type]
    )
    policy = values.get("policySummary") or {}
    policy = _mapping(policy, "policy summary")
    return GoogleDiagnosticObservationDTO(
        asset_type=spec.asset_type,
        asset_ref=asset_ref,
        configured_status=values.get("status"),
        primary_status=values.get("primaryStatus"),
        status_reasons=tuple(values.get("primaryStatusReasons") or ()),
        policy_approval_status=policy.get("approvalStatus") or "",
        policy_review_status=policy.get("reviewStatus") or "",
        policy_topics=tuple(policy.get("policyTopicEntries") or ()),
        observed_at=observed_at,
    )


def _observation_chunks(
    kind,
    items,
    *,
    current_cursor,
    next_cursor,
    final_has_more,
    request_id,
    reporting_context_hash,
    watermark,
    chunk_size,
):
    chunks = [
        items[index : index + chunk_size] for index in range(0, len(items), chunk_size)
    ] or [()]
    pages = []
    for index, chunk in enumerate(chunks):
        terminal = index == len(chunks) - 1
        pages.append(
            GoogleObservationPage(
                observation_kind=kind,
                items=tuple(chunk),
                next_cursor=next_cursor if terminal else current_cursor,
                has_more=final_has_more if terminal else True,
                provider_request_id=request_id,
                reporting_context_hash=reporting_context_hash,
                watermark=watermark,
            )
        )
    return tuple(pages)


def _next_diagnostic_cursor(stage, next_page_token):
    if next_page_token:
        return encode_google_diagnostic_cursor(stage, next_page_token), True
    index = GOOGLE_DIAGNOSTIC_STAGES.index(stage)
    if index + 1 >= len(GOOGLE_DIAGNOSTIC_STAGES):
        return "", False
    return encode_google_diagnostic_cursor(GOOGLE_DIAGNOSTIC_STAGES[index + 1]), True


def _provider_local_datetime(value, report_timezone):
    value = _text(value, "change timestamp", 64)
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("T", " "))
    except ValueError:
        raise GoogleApiError("Google Ads change timestamp is invalid") from None
    if parsed.tzinfo:
        return parsed.astimezone(datetime.timezone.utc).replace(
            tzinfo=None, microsecond=0
        )
    try:
        localized = pytz.timezone(report_timezone).localize(parsed, is_dst=None)
    except (pytz.AmbiguousTimeError, pytz.NonExistentTimeError):
        raise GoogleApiError("Google Ads change timestamp is ambiguous") from None
    return localized.astimezone(pytz.UTC).replace(tzinfo=None, microsecond=0)


def _changed_fields(value):
    if value in (None, "", False):
        return ()
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, dict) and set(value) == {"paths"}:
        values = value["paths"]
    else:
        values = value
    if not isinstance(values, (list, tuple)):
        raise GoogleApiError("Google Ads changed fields are invalid")
    return _field_paths(tuple(values))


def _field_paths(values):
    if not isinstance(values, tuple) or len(values) > 256:
        raise GoogleApiError("Google Ads changed fields are invalid")
    normalized = []
    for value in values:
        value = str(value or "").strip()
        if not _FIELD_PATH_RE.fullmatch(value):
            raise GoogleApiError("Google Ads changed field is invalid")
        if value not in normalized:
            normalized.append(value)
    return tuple(sorted(normalized))


def _policy_topics(values):
    if not isinstance(values, tuple) or len(values) > 128:
        raise GoogleApiError("Google Ads policy topics are invalid")
    topics = []
    for item in values:
        item = _mapping(item, "policy topic")
        normalized = {
            "topic": _text(item.get("topic"), "policy topic", 256),
            "type": _enum(item.get("type"), "policy topic type"),
        }
        if normalized not in topics:
            topics.append(normalized)
    return tuple(sorted(topics, key=lambda item: (item["topic"], item["type"])))


def _enum_values(values, label):
    if not isinstance(values, tuple) or len(values) > 128:
        raise GoogleApiError("Google Ads %s are invalid" % label)
    return tuple(sorted({_enum(value, label) for value in values}))


def _actor_hash(value):
    if value in (None, ""):
        return ""
    value = _text(value, "change actor", 320).lower()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _customer_resource(value, customer_id, segment):
    value = _text(value, "resource name", 512)
    if not value.startswith("customers/%s/%s/" % (customer_id, segment)):
        raise GoogleApiError("Google Ads resource name is invalid")
    return value


def _mapping(value, label):
    if not isinstance(value, dict):
        raise GoogleApiError("Google Ads %s is invalid" % label)
    return value


def _enum(value, label):
    value = _text(value, label, 128).upper()
    if not _ENUM_RE.fullmatch(value):
        raise GoogleApiError("Google Ads %s is invalid" % label)
    return value.lower()


def _optional_enum(value, label):
    return _enum(value, label) if value else ""


def _text(value, label, limit):
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


def _optional_text(value, label, limit):
    return _text(value, label, limit) if value else ""


def _utc_datetime(value, label):
    if not isinstance(value, datetime.datetime):
        raise GoogleApiError("Google Ads %s is invalid" % label)
    if value.tzinfo:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)


def _date(value, label):
    if isinstance(value, datetime.datetime):
        raise GoogleApiError("Google Ads %s is invalid" % label)
    if isinstance(value, datetime.date):
        return value
    if not isinstance(value, str):
        raise GoogleApiError("Google Ads %s is invalid" % label)
    try:
        parsed = datetime.date.fromisoformat(value)
    except ValueError:
        raise GoogleApiError("Google Ads %s is invalid" % label) from None
    if parsed.isoformat() != value:
        raise GoogleApiError("Google Ads %s is invalid" % label)
    return parsed


def _timezone(value):
    value = str(value or "").strip()
    if value not in pytz.all_timezones_set:
        raise GoogleApiError("Google Ads timezone is invalid")
    return value


def _page_token(value):
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise GoogleApiError("Google Ads observation cursor is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise GoogleApiError("Google Ads observation cursor is invalid") from None
    if len(encoded) > _MAX_PROVIDER_TOKEN_BYTES or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise GoogleApiError("Google Ads observation cursor is invalid")
    return value


_DIAGNOSTIC_SPECS = {
    "campaign": GoogleDiagnosticSpec(
        asset_type="campaign",
        row_key="campaign",
        query=" ".join(
            """
            SELECT campaign.resource_name, campaign.status,
                   campaign.primary_status, campaign.primary_status_reasons
              FROM campaign
             ORDER BY campaign.id
            """.split()
        ),
    ),
    "ad_group": GoogleDiagnosticSpec(
        asset_type="ad_group",
        row_key="adGroup",
        query=" ".join(
            """
            SELECT ad_group.resource_name, ad_group.status,
                   ad_group.primary_status, ad_group.primary_status_reasons
              FROM ad_group
             ORDER BY ad_group.id
            """.split()
        ),
    ),
    "ad": GoogleDiagnosticSpec(
        asset_type="ad",
        row_key="adGroupAd",
        query=" ".join(
            """
            SELECT ad_group_ad.resource_name, ad_group_ad.status,
                   ad_group_ad.primary_status,
                   ad_group_ad.primary_status_reasons,
                   ad_group_ad.policy_summary.approval_status,
                   ad_group_ad.policy_summary.review_status,
                   ad_group_ad.policy_summary.policy_topic_entries
              FROM ad_group_ad
             ORDER BY ad_group_ad.ad.id
            """.split()
        ),
    ),
}

_RESOURCE_SEGMENT = {
    "campaign": "campaigns",
    "ad_group": "adGroups",
    "ad": "adGroupAds",
}

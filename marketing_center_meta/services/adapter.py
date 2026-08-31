import dataclasses
import re

from odoo.addons.meta_api_base.services.errors import MetaApiError, MetaApiPausedError
from odoo.addons.meta_api_base.services.graph import graph_debug_token, graph_request

from .catalog import fetch_meta_catalog_page
from .credentials import MetaCredentialResolutionError, resolve_profile_credentials

META_ADAPTER_KEY = "meta.graph"
META_ADS_SERVICE = "meta.ads"
_AD_ACCOUNT_RE = re.compile(r"^act_[0-9]+$")
_ACCOUNT_ID_RE = re.compile(r"^[0-9]+$")
_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_MAX_DISCOVERY_PAGES = 20


@dataclasses.dataclass(frozen=True)
class MetaReadValidation:
    token_type: str
    expires_at: int
    data_access_expires_at: int
    scopes: tuple
    capabilities: dict


@dataclasses.dataclass(frozen=True)
class MetaAdAccount:
    external_ref: str
    external_id: str
    name: str
    currency: str
    timezone: str
    account_status: int
    disable_reason: int


class _GraphApp:
    def __init__(self, profile, app_secret):
        self.active = profile.active
        self.external_app_id = profile.external_app_id
        self.graph_version = profile.graph_version
        self.app_secret = app_secret

    def ensure_one(self):
        return self


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


def _bounded_integer(value, label):
    if isinstance(value, bool):
        raise MetaApiError("Meta %s is invalid" % label)
    try:
        result = int(value or 0)
    except (TypeError, ValueError):
        raise MetaApiError("Meta %s is invalid" % label) from None
    if result < 0 or result > 2**63 - 1:
        raise MetaApiError("Meta %s is invalid" % label)
    return result


def _normalized_scopes(values):
    if not isinstance(values, list):
        raise MetaApiPausedError("Meta authorization scopes are unavailable")
    scopes = []
    for value in values:
        scope = _bounded_text(value, "authorization scope", 128).lower()
        if not _SCOPE_RE.fullmatch(scope):
            raise MetaApiError("Meta authorization scope is invalid")
        if scope not in scopes:
            scopes.append(scope)
    return tuple(sorted(scopes))


def _capabilities(scopes):
    scope_set = set(scopes)
    return {
        "read_entities": "ads_read" in scope_set,
        "read_metrics": "ads_read" in scope_set,
        "receive_leads": "leads_retrieval" in scope_set,
    }


def _ad_account(value):
    if not isinstance(value, dict):
        raise MetaApiError("Meta ad account response is invalid")
    external_ref = _bounded_text(value.get("id"), "ad account reference", 64)
    external_id = _bounded_text(value.get("account_id"), "ad account ID", 32)
    if not _AD_ACCOUNT_RE.fullmatch(external_ref) or not _ACCOUNT_ID_RE.fullmatch(
        external_id
    ):
        raise MetaApiError("Meta ad account identity is invalid")
    if external_ref != "act_%s" % external_id:
        raise MetaApiError("Meta ad account identity is inconsistent")
    currency = _bounded_text(value.get("currency"), "ad account currency", 3).upper()
    if len(currency) != 3 or not currency.isalpha():
        raise MetaApiError("Meta ad account currency is invalid")
    return MetaAdAccount(
        external_ref=external_ref,
        external_id=external_id,
        name=_bounded_text(value.get("name"), "ad account name", 256),
        currency=currency,
        timezone=_bounded_text(value.get("timezone_name"), "ad account timezone", 64),
        account_status=_bounded_integer(value.get("account_status"), "account status"),
        disable_reason=_bounded_integer(value.get("disable_reason"), "disable reason"),
    )


class MetaMarketingReadAdapter:
    """Credential-safe read-only boundary over ``meta_api_base``."""

    def __init__(self, profile):
        profile.ensure_one()
        self.profile = profile
        try:
            credentials = resolve_profile_credentials(profile)
        except MetaCredentialResolutionError as error:
            raise MetaApiPausedError(str(error)) from None
        self._access_token = credentials.access_token
        self._app = _GraphApp(profile, credentials.app_secret)

    def validate(self):
        payload = graph_debug_token(self._app, self._access_token)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or data.get("is_valid") is not True:
            raise MetaApiPausedError("Meta authorization is invalid")
        app_id = _bounded_text(data.get("app_id"), "authorization App ID", 64)
        if app_id != self.profile.external_app_id:
            raise MetaApiPausedError("Meta authorization belongs to another App")
        scopes = _normalized_scopes(data.get("scopes"))
        missing = set(self.profile.required_scope_keys()) - set(scopes)
        if missing:
            raise MetaApiPausedError("Meta authorization scope is unavailable")
        capabilities = _capabilities(scopes)
        if not capabilities["read_entities"]:
            raise MetaApiPausedError("Meta Ads read capability is unavailable")
        return MetaReadValidation(
            token_type=_bounded_text(
                data.get("type") or "unknown",
                "authorization type",
                64,
            ).lower(),
            expires_at=_bounded_integer(data.get("expires_at"), "token expiry"),
            data_access_expires_at=_bounded_integer(
                data.get("data_access_expires_at"),
                "data access expiry",
            ),
            scopes=scopes,
            capabilities=capabilities,
        )

    def discover_ad_accounts(self):
        accounts = {}
        after = ""
        seen_cursors = set()
        for _page_number in range(_MAX_DISCOVERY_PAGES):
            params = {
                "fields": (
                    "id,account_id,name,currency,timezone_name,account_status,"
                    "disable_reason"
                ),
                "limit": 100,
            }
            if after:
                params["after"] = after
            payload = graph_request(
                self._app,
                self._access_token,
                "GET",
                "me/adaccounts",
                params=params,
                max_response_bytes=512 * 1024,
            )
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise MetaApiError("Meta ad account response is invalid")
            for row in rows:
                account = _ad_account(row)
                previous = accounts.get(account.external_ref)
                if previous and previous != account:
                    raise MetaApiError("Meta ad account response is inconsistent")
                accounts[account.external_ref] = account
            raw_paging = payload.get("paging")
            if raw_paging not in (None, False) and not isinstance(raw_paging, dict):
                raise MetaApiError("Meta ad account pagination is invalid")
            paging = raw_paging or {}
            raw_cursors = paging.get("cursors")
            if raw_cursors not in (None, False) and not isinstance(raw_cursors, dict):
                raise MetaApiError("Meta ad account pagination is invalid")
            cursors = raw_cursors or {}
            next_page = paging.get("next")
            if not next_page:
                return tuple(accounts[key] for key in sorted(accounts))
            if not isinstance(next_page, str):
                raise MetaApiError("Meta ad account pagination is invalid")
            next_after = cursors.get("after") if isinstance(cursors, dict) else ""
            after = _bounded_text(
                next_after,
                "ad account cursor",
                512,
                required=False,
            )
            if not after or after in seen_cursors:
                raise MetaApiError("Meta ad account pagination is invalid")
            seen_cursors.add(after)
        raise MetaApiError("Meta ad account discovery exceeded the page limit")

    def fetch_catalog_page(
        self,
        account_ref,
        entity_type,
        *,
        after="",
        reporting_context_hash,
    ):
        return fetch_meta_catalog_page(
            self._app,
            self._access_token,
            account_ref,
            entity_type,
            after=after,
            reporting_context_hash=reporting_context_hash,
        )

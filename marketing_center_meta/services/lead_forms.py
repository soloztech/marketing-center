"""Bounded, credential-safe discovery of a Page's Meta Instant Forms."""

import datetime
import re
from dataclasses import dataclass

from odoo.addons.meta_api_base.services.credentials import MetaCredentialResolutionError
from odoo.addons.meta_api_base.services.errors import MetaApiError
from odoo.addons.meta_api_base.services.graph import graph_request
from odoo.addons.meta_webhook_base.services.tokens import META_WEBHOOK_RUNTIME_TOKEN

from .datetime_utils import parse_meta_datetime
from .graph_contract import require_marketing_graph_version

META_FORM_FIELDS = "id,name,status,leads_count,created_time"
_PAGE_LIMIT = 100
_MAX_PAGES = 20
_MAX_CURSOR_BYTES = 3072
_ID_RE = re.compile(r"^[0-9]{1,40}$")
_STATUS_RE = re.compile(r"^[A-Z][A-Z_]{0,31}$")


@dataclass(frozen=True)
class MetaLeadForm:
    external_form_id: str
    name: str
    status: str
    leads_count: int
    created_at: datetime.datetime | None


class MetaLeadFormsReadAdapter:
    """Resolve the existing Page token; this edge does not accept the SU token."""

    def __init__(self, page, *, expected_page_revision, expected_app_revision):
        page.ensure_one()
        try:
            self._app, self._access_token, self._page_revision = page.with_context(
                meta_webhook_runtime=META_WEBHOOK_RUNTIME_TOKEN
            )._resolve_graph_runtime(
                expected_page_revision=expected_page_revision,
                expected_app_revision=expected_app_revision,
            )
        except MetaCredentialResolutionError:
            raise MetaApiError("Meta Page credentials are unavailable") from None
        self._page_id = page.external_page_id

    def discover_lead_forms(self, page_id):
        if page_id != self._page_id:
            raise MetaApiError("Meta Page discovery scope changed")
        return fetch_meta_lead_forms(self._app, self._access_token, page_id)


def fetch_meta_lead_forms(app, access_token, page_id):
    """Return a complete bounded listing, or fail without partial results.

    Only Page form metadata is requested. Neither lead answers nor credentials
    enter the return value. Pagination URLs are never followed or persisted.
    """
    require_marketing_graph_version(getattr(app, "graph_version", ""), "lead_ads")
    page_id = _text(page_id, 40)
    if not _ID_RE.fullmatch(page_id):
        raise MetaApiError("Meta Page identity is invalid")
    forms = {}
    after = ""
    seen_cursors = set()
    for _page_number in range(_MAX_PAGES):
        params = {"fields": META_FORM_FIELDS, "limit": _PAGE_LIMIT}
        if after:
            params["after"] = after
        payload = graph_request(
            app,
            access_token,
            "GET",
            "%s/leadgen_forms" % page_id,
            params=params,
            max_response_bytes=512 * 1024,
        )
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) > _PAGE_LIMIT:
            raise MetaApiError("Meta form listing is invalid")
        for row in rows:
            form = _form(row)
            previous = forms.get(form.external_form_id)
            if previous and previous != form:
                raise MetaApiError("Meta form listing contains conflicting identities")
            forms[form.external_form_id] = form
        paging = payload.get("paging")
        if paging is None or paging is False:
            paging = {}
        if not isinstance(paging, dict):
            raise MetaApiError("Meta form pagination is invalid")
        next_url = paging.get("next")
        if not next_url:
            return tuple(forms[key] for key in sorted(forms))
        if not isinstance(next_url, str):
            raise MetaApiError("Meta form pagination is invalid")
        cursors = paging.get("cursors")
        if not isinstance(cursors, dict):
            raise MetaApiError("Meta form pagination is invalid")
        after = _text(cursors.get("after"), _MAX_CURSOR_BYTES)
        if after in seen_cursors:
            raise MetaApiError("Meta form pagination contains a cycle")
        seen_cursors.add(after)
    raise MetaApiError("Meta form discovery exceeded the page limit")


def _form(value):
    if not isinstance(value, dict):
        raise MetaApiError("Meta form metadata is invalid")
    form_id = _text(value.get("id"), 40)
    if not _ID_RE.fullmatch(form_id):
        raise MetaApiError("Meta form identity is invalid")
    name = _text(value.get("name"), 512)
    status = _text(value.get("status"), 32)
    if not _STATUS_RE.fullmatch(status):
        raise MetaApiError("Meta form status is invalid")
    count = value.get("leads_count")
    if type(count) is not int or not 0 <= count <= 2147483647:
        raise MetaApiError("Meta form count is invalid")
    created_at = None
    if value.get("created_time") is not None:
        try:
            created_at = parse_meta_datetime(_text(value["created_time"], 64))
        except (TypeError, ValueError):
            raise MetaApiError("Meta form creation date is invalid") from None
    return MetaLeadForm(form_id, name, status, count, created_at)


def _text(value, limit):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > limit
        or any(ord(character) < 32 for character in value)
    ):
        raise MetaApiError("Meta form metadata is invalid")
    return value.strip()

"""One bounded read of an authorized ad and its actual creative content.

Fields follow Meta's official Business SDK Ad/AdCreative/ObjectStorySpec schema.
Signed thumbnails are an internal locator and must never be sent to an operator DTO.
"""

import re
from urllib.parse import urlsplit

from odoo.addons.marketing_center_base.services.dto import (
    AttributionDTOValidationError,
    sanitize_url,
)
from odoo.addons.meta_api_base.services.errors import MetaApiError
from odoo.addons.meta_api_base.services.graph import graph_request

from .graph_contract import require_marketing_graph_version

_AD_REF = re.compile(r"^(act_([0-9]{1,32}))/ads/([0-9]{1,32})$")
_OBJECT_ID = re.compile(r"^[0-9]{1,32}$")
_FIELDS = (
    "id,account_id,creative{id,account_id,title,body,object_url,link_url,"
    "instagram_permalink_url,thumbnail_url,image_url,video_id,object_story_spec}"
)


def _text(value, limit):
    if not isinstance(value, str) or any(
        ord(char) == 0 or 0xD800 <= ord(char) <= 0xDFFF for char in value
    ):
        return ""
    return value.strip()[:limit]


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _public_url(*candidates):
    for value in candidates:
        try:
            result = sanitize_url(value, "ad preview URL")
        except AttributionDTOValidationError:
            continue
        if result:
            return result
    return ""


def _thumbnail_url(*candidates):
    for value in candidates:
        if not isinstance(value, str) or not value or len(value) > 4096:
            continue
        if any(ord(char) <= 32 or ord(char) == 127 for char in value):
            continue
        try:
            parts = urlsplit(value)
            valid = (
                parts.scheme == "https"
                and parts.hostname
                and parts.username is None
                and parts.password is None
                and parts.port in (None, 443)
                and not parts.fragment
            )
        except ValueError:
            continue
        if valid:
            # Query signatures stay internal until the Contact Center vault and
            # protected downloader validate/store the locator and resulting image.
            return value
    return ""


def fetch_meta_ad_preview(app, access_token, account_ref, ad_ref):
    identity = _AD_REF.fullmatch(ad_ref) if isinstance(ad_ref, str) else None
    if not identity or identity.group(1) != account_ref:
        raise MetaApiError("Meta ad preview identity is invalid")
    require_marketing_graph_version(app.graph_version, "ad_preview")
    payload = graph_request(
        app,
        access_token,
        "GET",
        identity.group(3),
        params={"fields": _FIELDS},
        max_response_bytes=128 * 1024,
    )
    if (
        not isinstance(payload, dict)
        or payload.get("id") != identity.group(3)
        or payload.get("account_id") != identity.group(2)
    ):
        raise MetaApiError("Meta ad preview identity is inconsistent")
    creative = _mapping(payload.get("creative"))
    if not creative:
        return {}
    if (
        not isinstance(creative.get("id"), str)
        or not _OBJECT_ID.fullmatch(creative["id"])
        or creative.get("account_id") != identity.group(2)
    ):
        raise MetaApiError("Meta ad preview creative identity is inconsistent")
    story = _mapping(creative.get("object_story_spec"))
    link = _mapping(story.get("link_data"))
    video = _mapping(story.get("video_data"))
    photo = _mapping(story.get("photo_data"))
    action = _mapping(_mapping(video.get("call_to_action")).get("value"))
    values = {
        "title": _text(creative.get("title"), 256)
        or _text(link.get("name"), 256)
        or _text(video.get("title"), 256),
        "body": _text(creative.get("body"), 2000)
        or _text(link.get("message"), 2000)
        or _text(video.get("message"), 2000)
        or _text(photo.get("caption"), 2000),
        "public_url": _public_url(
            creative.get("instagram_permalink_url"),
            link.get("link"),
            creative.get("object_url"),
            creative.get("link_url"),
            action.get("link"),
        ),
        "thumbnail_url": _thumbnail_url(
            creative.get("thumbnail_url"),
            creative.get("image_url"),
            link.get("picture"),
            video.get("image_url"),
            photo.get("url"),
        ),
    }
    if values["thumbnail_url"]:
        values["media_type"] = "video" if creative.get("video_id") or video else "image"
    # Internal ad/creative names and arbitrary dynamic-creative variants are not
    # customer-facing ad copy and are deliberately not used as fallbacks.
    return {key: value for key, value in values.items() if value}

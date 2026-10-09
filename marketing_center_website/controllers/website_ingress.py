import json
from urllib.parse import urlsplit

from werkzeug.wrappers import Response

from odoo import http
from odoo.http import request

from ..services.ingress.contracts import (
    WebIngressContractError,
    normalize_allowed_hosts,
    normalize_allowed_origins,
    normalize_origin,
)


def _endpoint_origin_allowed(endpoint, host_url):
    """Do not advertise capture on a fallback Website served on another host."""
    try:
        return bool(
            normalize_origin(host_url)
            in normalize_allowed_origins(endpoint.allowed_origins)
            and urlsplit(host_url).hostname
            in normalize_allowed_hosts(endpoint.allowed_hosts)
        )
    except (WebIngressContractError, ValueError):
        return False


def _config_response(payload):
    return Response(
        json.dumps(payload, separators=(",", ":")),
        status=200,
        content_type="application/json; charset=utf-8",
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Content-Security-Policy": "default-src 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _public_binding(req=None):
    req = request if req is None else req
    if not req.env.user._is_public():
        return req.env["marketing.website.ingress.binding"]
    return (
        req.env["marketing.website.ingress.binding"]
        .sudo()
        .search(
            [("website_id", "=", req.website.id), ("active", "=", True)],
            limit=1,
        )
    )


def _ingress_configuration(binding):
    if not request.env.user._is_public():
        return {"enabled": False}
    website = request.website
    endpoint = binding.endpoint_id
    if (
        not binding
        or not endpoint.active
        or not endpoint._capture_policy_allows()
        or endpoint.company_id != website.company_id
        or not _endpoint_origin_allowed(endpoint, request.httprequest.host_url)
        or (
            (
                endpoint._requires_individual_consent()
                or endpoint._informational_notice()
            )
            and request.httprequest.scheme != "https"
        )
    ):
        return {"enabled": False}
    if binding.capture_mode == "native":
        return {"enabled": True, "capture_mode": "native"}
    return {
        "enabled": True,
        "capture_mode": binding.capture_mode,
        "ingest_path": "/marketing/web-ingress/%s" % endpoint.public_ref,
        "public_key": endpoint.public_key,
        "config_revision": endpoint.config_revision,
        "informational_notice": endpoint._informational_notice(),
        "capture_allowed": True,
        **(
            {
                "consent_ref": request.env["marketing.website.consent"]
                ._current(endpoint)
                .public_ref
            }
            if endpoint._requires_individual_consent()
            else {}
        ),
    }


class MarketingWebsiteIngressController(http.Controller):
    @http.route(
        "/marketing/website-ingress/config",
        type="http",
        auth="public",
        methods=["GET"],
        website=True,
        sitemap=False,
        multilang=False,
        save_session=False,
    )
    def public_config(self, **_kwargs):
        return _config_response(_ingress_configuration(_public_binding()))

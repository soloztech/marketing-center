import json
from urllib.parse import urlsplit

from werkzeug.wrappers import Response

from odoo import http
from odoo.http import request

from odoo.addons.marketing_center_web_ingress.services.contracts import (
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
        if not request.env.user._is_public():
            return _config_response({"enabled": False})
        website = request.website
        binding = (
            request.env["marketing.website.ingress.binding"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("website_id", "=", website.id),
                    ("active", "=", True),
                ],
                limit=1,
            )
        )
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
            return _config_response({"enabled": False})
        if binding.capture_mode == "native":
            return _config_response({"enabled": True, "capture_mode": "native"})
        return _config_response(
            {
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
        )

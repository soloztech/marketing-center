import json

from werkzeug.wrappers import Response

from odoo import http
from odoo.http import request


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
        ):
            return _config_response({"enabled": False})
        return _config_response(
            {
                "enabled": True,
                "ingest_path": "/marketing/web-ingress/%s" % endpoint.public_ref,
                "public_key": endpoint.public_key,
                "config_revision": endpoint.config_revision,
            }
        )

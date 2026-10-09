"""One binding lookup; each section retains its legacy authorization policy."""

from odoo import http

from .website_consent import _binding, _configuration
from .website_ingress import _config_response, _ingress_configuration, _public_binding


class MarketingWebsiteBootstrapController(http.Controller):
    @http.route(
        "/marketing/website/bootstrap-config",
        type="http",
        auth="public",
        methods=["GET"],
        website=True,
        sitemap=False,
        multilang=False,
        save_session=False,
    )
    def configuration(self, **_kwargs):
        binding = _public_binding()
        return _config_response(
            {
                "schema_version": 1,
                "ingress": _ingress_configuration(binding),
                "consent": _configuration(_binding(binding)),
            }
        )

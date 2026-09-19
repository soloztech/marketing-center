import logging
from urllib.parse import urlsplit

from psycopg2.errors import DeadlockDetected, SerializationFailure

from odoo import fields, http, models
from odoo.http import request

from odoo.addons.marketing_center_web_ingress.services.contracts import (
    normalize_allowed_hosts,
    normalize_allowed_origins,
    normalize_origin,
)

from ..services.geolocation import request_observation

_logger = logging.getLogger(__name__)
GEO_FIELDS = (
    "marketing_ip_address",
    "marketing_ip_observed_at",
    "marketing_geo_country_id",
    "marketing_geo_state_id",
    "marketing_geo_city",
    "marketing_geo_timezone",
    "marketing_geo_source",
)


def geoip_lookup(address):
    resolver = http.root.geoip_resolver
    return resolver.resolve(address) if resolver else {}


class WebsiteVisitor(models.Model):
    _inherit = "website.visitor"

    marketing_ip_address = fields.Char(
        string="IP observado",
        size=45,
        readonly=True,
        copy=False,
        groups="base.group_user",
    )
    marketing_ip_observed_at = fields.Datetime(
        string="Data da observação", readonly=True, copy=False, groups="base.group_user"
    )
    marketing_geo_country_id = fields.Many2one(
        "res.country",
        string="País estimado",
        readonly=True,
        copy=False,
        groups="base.group_user",
    )
    marketing_geo_state_id = fields.Many2one(
        "res.country.state",
        string="Estado estimado",
        readonly=True,
        copy=False,
        groups="base.group_user",
    )
    marketing_geo_city = fields.Char(
        string="Cidade estimada",
        size=128,
        readonly=True,
        copy=False,
        groups="base.group_user",
    )
    marketing_geo_timezone = fields.Char(
        string="Fuso estimado",
        size=64,
        readonly=True,
        copy=False,
        groups="base.group_user",
    )
    marketing_geo_source = fields.Selection(
        [("cloudflare", "Cloudflare"), ("geoip", "GeoIP do Odoo")],
        string="Origem da localização",
        readonly=True,
        copy=False,
        groups="base.group_user",
    )

    def _marketing_request_observation(self):
        if not request or not getattr(request, "website", None):
            return {}
        params = self.env["ir.config_parameter"].sudo()
        if params.get_param("marketing_center_website.ip_enrichment_enabled") != "True":
            return {}
        website = request.website
        binding = website._marketing_measurement_binding()
        if (
            not binding
            or binding.capture_mode != "native"
            or not binding.endpoint_id._capture_policy_allows()
            or request.httprequest.scheme != "https"
            or request.env.user._is_internal()
        ):
            return {}
        endpoint = binding.endpoint_id
        origin = request.httprequest.host_url
        if normalize_origin(origin) not in normalize_allowed_origins(
            endpoint.allowed_origins
        ) or urlsplit(origin).hostname not in normalize_allowed_hosts(
            endpoint.allowed_hosts
        ):
            return {}
        observation = request_observation(
            request.httprequest.environ,
            request.httprequest.headers,
            params.get_param("marketing_center_website.geo_proxy_networks", ""),
            geoip_lookup,
        )
        if not observation:
            return {}
        country = (
            self.env["res.country"]
            .sudo()
            .search([("code", "=", observation.get("country_code"))], limit=1)
            if observation.get("country_code")
            else self.env["res.country"]
        )
        state = (
            self.env["res.country.state"]
            .sudo()
            .search(
                [
                    ("country_id", "=", country.id),
                    ("code", "=", observation.get("region")),
                ],
                limit=1,
            )
            if country and observation.get("region")
            else self.env["res.country.state"]
        )
        return {
            "marketing_ip_address": observation["ip"],
            "marketing_ip_observed_at": fields.Datetime.now(),
            "marketing_geo_country_id": country.id or False,
            "marketing_geo_state_id": state.id or False,
            "marketing_geo_city": observation.get("city") or False,
            "marketing_geo_timezone": observation.get("timezone") or False,
            "marketing_geo_source": observation.get("source") or False,
        }

    def _marketing_observe(self, values):
        """Keep a single latest observation, with no touchpoint/event per visit."""
        self.ensure_one()
        values = {name: values.get(name, False) for name in GEO_FIELDS}
        # Native country/timezone may already describe an identified visitor.
        # Do not replace them with a network estimate or change partner fields.
        if not self.country_id and values["marketing_geo_country_id"]:
            values["country_id"] = values["marketing_geo_country_id"]
        if not self.timezone and values["marketing_geo_timezone"]:
            values["timezone"] = values["marketing_geo_timezone"]
        self.sudo().write(values)

    def _get_visitor_from_request(self, force_create=False, force_track_values=None):
        visitor = super()._get_visitor_from_request(
            force_create=force_create, force_track_values=force_track_values
        )
        if force_create and visitor:
            try:
                with self.env.cr.savepoint():
                    values = self._marketing_request_observation()
                    if values:
                        visitor._marketing_observe(values)
            except (SerializationFailure, DeadlockDetected):
                raise
            except Exception as error:
                _logger.warning(
                    "Visitor IP enrichment unavailable (%s)", type(error).__name__
                )
        return visitor

    def _merge_visitor(self, target):
        self.ensure_one()
        if self.marketing_ip_observed_at and (
            not target.marketing_ip_observed_at
            or self.marketing_ip_observed_at > target.marketing_ip_observed_at
        ):
            target._marketing_observe(
                {
                    name: self[name].id if name.endswith("_id") else self[name]
                    for name in GEO_FIELDS
                }
            )
        return super()._merge_visitor(target)

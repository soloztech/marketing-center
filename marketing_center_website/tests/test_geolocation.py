import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from odoo import fields, http
from odoo.tests.common import SavepointCase

from ..models import website_visitor
from ..services.geolocation import request_observation


class TestWebsiteGeolocation(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.website = cls.env["website"].create(
            {
                "name": "IP observation test",
                "domain": "https://ip.example.test",
                "company_id": cls.env.company.id,
            }
        )
        cls.endpoint = cls.env["marketing.web.ingress.endpoint"].create(
            {
                "name": "IP observation test",
                "company_id": cls.env.company.id,
                "allowed_origins": cls.website.domain,
                "allowed_hosts": "ip.example.test",
                "website_tracking_policy": "informational_notice",
                "capture_enabled": True,
                "capture_purpose": "website_attribution",
                "privacy_policy_version": "ip-v1",
                "privacy_notice_version": "ip-v1",
                "privacy_legal_basis_code": False,
                "privacy_policy_justification": "Synthetic IP test",
                "identifier_retention_days": 30,
            }
        )
        cls.binding = cls.env["marketing.website.ingress.binding"].create(
            {
                "website_id": cls.website.id,
                "endpoint_id": cls.endpoint.id,
            }
        )
        cls.params = cls.env["ir.config_parameter"].sudo()
        cls.params.set_param("marketing_center_website.ip_enrichment_enabled", True)
        cls.params.set_param(
            "marketing_center_website.geo_proxy_networks", "127.0.0.1/32"
        )
        cls.brazil = cls.env.ref("base.br")
        cls.sp = cls.env["res.country.state"].search(
            [("country_id", "=", cls.brazil.id), ("code", "=", "SP")], limit=1
        )

    def setUp(self):
        super().setUp()
        self.headers = {
            "X-MC-Visitor-IP": "8.8.8.8",
            "X-MC-Geo-Source": "cloudflare",
            "X-MC-Geo-Country": "BR",
            "X-MC-Geo-Region": "SP",
            "X-MC-Geo-City": "São Paulo",
            "X-MC-Geo-Timezone": "America/Sao_Paulo",
        }
        self.environ = {
            "REMOTE_ADDR": "8.8.8.8",
            "werkzeug.proxy_fix.orig": {"REMOTE_ADDR": "127.0.0.1"},
        }

    def _parse(self, headers=None, environ=None, networks="127.0.0.1/32", lookup=None):
        return request_observation(
            self.environ if environ is None else environ,
            self.headers if headers is None else headers,
            networks,
            lookup or (lambda address: {}),
        )

    @contextmanager
    def _request(self, internal=False, host="ip.example.test"):
        website = self.website.with_user(
            self.env.user if internal else self.website.user_id
        )
        fake = SimpleNamespace(
            env=website.env,
            website=website,
            session=SimpleNamespace(uid=None),
            httprequest=SimpleNamespace(
                environ=self.environ,
                headers=self.headers,
                scheme="https",
                host_url="https://%s/" % host,
                cookies={},
            ),
        )
        http._request_stack.push(fake)
        try:
            yield fake
        finally:
            http._request_stack.pop()

    def test_trusted_cloudflare_atomic_geo_and_encoding(self):
        lookup = Mock(
            side_effect=AssertionError("Cloudflare already provided location")
        )
        for city in ("São Paulo", "S%C3%A3o%20Paulo", "SÃ£o Paulo"):
            result = self._parse(
                dict(self.headers, **{"X-MC-Geo-City": city}), lookup=lookup
            )
            self.assertEqual(
                result,
                {
                    "ip": "8.8.8.8",
                    "source": "cloudflare",
                    "country_code": "BR",
                    "region": "SP",
                    "city": "São Paulo",
                    "timezone": "America/Sao_Paulo",
                },
            )
        lookup.assert_not_called()

    def test_untrusted_forwarded_and_raw_cloudflare_headers_are_ignored(self):
        environ = {
            "REMOTE_ADDR": "8.8.8.8",
            "werkzeug.proxy_fix.orig": {"REMOTE_ADDR": "1.1.1.1"},
        }
        lookup = Mock(return_value={"country_code": "US", "city": "Fallback"})
        result = self._parse(environ=environ, lookup=lookup)
        self.assertEqual(result["ip"], "1.1.1.1")
        self.assertEqual(result["source"], "geoip")
        self.assertEqual(result["country_code"], "US")
        self.assertFalse(result["region"])
        lookup.assert_called_once_with("1.1.1.1")
        raw = {
            "CF-Connecting-IP": "8.8.8.8",
            "CF-IPCountry": "BR",
            "X-Forwarded-For": "8.8.8.8",
        }
        self.assertEqual(
            self._parse(headers=raw, environ={"REMOTE_ADDR": "192.168.2.45"}),
            {"ip": "192.168.2.45"},
        )

    def test_ipv6_private_missing_and_malformed(self):
        ipv6 = "2606:4700:4700:0:0:0:0:1111"
        result = self._parse(dict(self.headers, **{"X-MC-Visitor-IP": ipv6}))
        self.assertEqual(result["ip"], "2606:4700:4700::1111")
        for address in (
            "",
            "bad",
            "8.8.8.8,1.1.1.1",
            "::",
            "224.0.0.1",
            "fe80::1%eth0",
        ):
            self.assertEqual(
                self._parse(dict(self.headers, **{"X-MC-Visitor-IP": address})), {}
            )
        lookup = Mock(
            side_effect=AssertionError("Nonpublic addresses must not be located")
        )
        for address in ("192.168.2.45", "127.0.0.1", "2001:db8::1", "fd00::1"):
            self.assertEqual(
                self._parse(
                    dict(self.headers, **{"X-MC-Visitor-IP": address}), lookup=lookup
                ),
                {"ip": address},
            )
        lookup.assert_not_called()

    def test_geoip_failure_and_invalid_geo_are_nonblocking(self):
        headers = dict(self.headers, **{"X-MC-Geo-Country": "XX"})
        self.assertEqual(
            self._parse(headers, lookup=Mock(side_effect=RuntimeError())),
            {"ip": "8.8.8.8"},
        )
        headers.update(
            {
                "X-MC-Geo-Country": "BR",
                "X-MC-Geo-City": "bad%00city",
                "X-MC-Geo-Timezone": "invalid",
                "X-MC-Geo-Region": "bad region",
            }
        )
        result = self._parse(headers)
        self.assertFalse(result["city"])
        self.assertFalse(result["region"])
        self.assertFalse(result["timezone"])

    def test_invalid_network_config_never_trusts_headers(self):
        self.assertEqual(self._parse(networks="invalid"), {"ip": "127.0.0.1"})
        self.assertEqual(self._parse(networks=""), {"ip": "127.0.0.1"})

    def test_observation_policy_and_opt_in(self):
        with self._request():
            values = self.env["website.visitor"]._marketing_request_observation()
            self.assertEqual(values["marketing_geo_country_id"], self.brazil.id)
            self.assertEqual(values["marketing_geo_state_id"], self.sp.id)
            self.params.set_param(
                "marketing_center_website.ip_enrichment_enabled", False
            )
            self.assertEqual(
                self.env["website.visitor"]._marketing_request_observation(), {}
            )
            self.params.set_param(
                "marketing_center_website.ip_enrichment_enabled", True
            )
            for record, field, value in (
                (self.binding, "active", False),
                (self.binding, "capture_mode", "legacy"),
                (self.endpoint, "capture_enabled", False),
            ):
                before = record[field]
                record[field] = value
                self.assertEqual(
                    self.env["website.visitor"]._marketing_request_observation(), {}
                )
                record[field] = before
            self.endpoint.write(
                {
                    "website_tracking_policy": "individual_consent",
                    "privacy_legal_basis_code": "consent",
                }
            )
            self.assertEqual(
                self.env["website.visitor"]._marketing_request_observation(), {}
            )

    def test_internal_and_wrong_host_do_not_capture(self):
        for kwargs in ({"internal": True}, {"host": "other.example.test"}):
            with self._request(**kwargs):
                self.assertEqual(
                    self.env["website.visitor"]._marketing_request_observation(), {}
                )

    def test_geoip_current_ip_does_not_use_session_location(self):
        self.headers["X-MC-Geo-Source"] = ""
        with self._request() as fake, patch.object(
            website_visitor,
            "geoip_lookup",
            return_value={"country_code": "BR", "region": "SP", "city": "Current city"},
        ) as lookup:
            fake.session["_geoip"] = {"country_code": "US", "city": "Old city"}
            values = self.env["website.visitor"]._marketing_request_observation()
            self.assertEqual(values["marketing_geo_city"], "Current city")
            self.assertEqual(values["marketing_geo_source"], "geoip")
            lookup.assert_called_once_with("8.8.8.8")

    def test_latest_observation_preserves_native_and_partner_location(self):
        partner = self.env["res.partner"].create(
            {
                "name": "Synthetic IP contact",
                "country_id": self.env.ref("base.us").id,
                "city": "Customer city",
            }
        )
        visitor = self.env["website.visitor"].create(
            {
                "access_token": str(partner.id),
                "country_id": partner.country_id.id,
                "timezone": "UTC",
            }
        )
        with self._request():
            values = visitor._marketing_request_observation()
        visitor._marketing_observe(values)
        self.assertEqual(visitor.country_id, partner.country_id)
        self.assertEqual(visitor.timezone, "UTC")
        self.assertEqual(visitor.marketing_geo_country_id, self.brazil)
        self.assertEqual(partner.city, "Customer city")
        visitor._marketing_observe(
            {
                "marketing_ip_address": "192.168.2.45",
                "marketing_ip_observed_at": fields.Datetime.now(),
            }
        )
        self.assertFalse(visitor.marketing_geo_city)
        self.assertFalse(visitor.marketing_geo_source)
        self.assertEqual(visitor.country_id, partner.country_id)

    def test_empty_native_fields_can_use_estimate(self):
        visitor = self.env["website.visitor"].create({"access_token": uuid.uuid4().hex})
        with self._request():
            visitor._marketing_observe(visitor._marketing_request_observation())
        self.assertEqual(visitor.country_id, self.brazil)
        self.assertEqual(visitor.timezone, "America/Sao_Paulo")
        public = visitor.with_user(self.website.user_id)
        self.assertNotIn("marketing_ip_address", public.fields_get())

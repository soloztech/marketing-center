import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from lxml import etree

from odoo.tests.common import SavepointCase

from ..models import website as website_module


class TestMarketingWebsiteMeasurementConfig(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.website = cls.env["website"].create(
            {
                "name": "Generic measurement site",
                "domain": "https://measurement.example.test",
                "company_id": cls.env.company.id,
                "cookies_bar": True,
            }
        )
        cls.endpoint = cls.env["marketing.web.ingress.endpoint"].create(
            {
                "name": "Measurement configuration endpoint",
                "company_id": cls.env.company.id,
                "allowed_origins": cls.website.domain,
                "allowed_hosts": "measurement.example.test",
                "capture_enabled": True,
                "capture_purpose": "website_attribution",
                "privacy_policy_version": "test-v1",
                "privacy_notice_version": "test-v1",
                "privacy_legal_basis_code": "consent",
                "privacy_policy_justification": "Synthetic test",
                "identifier_retention_days": 30,
            }
        )
        cls.binding = cls.env["marketing.website.ingress.binding"].create(
            {
                "website_id": cls.website.id,
                "endpoint_id": cls.endpoint.id,
            }
        )
        cls.view = cls.env["ir.ui.view"].create(
            {
                "name": "Measurement public page",
                "type": "qweb",
                "key": "marketing_center_website.test_measurement_public_page",
                "arch_db": '<t t-name="marketing_center_website.test_measurement_public_page">'
                "<div>Public</div></t>",
            }
        )
        cls.page = cls.env["website.page"].create(
            {
                "name": "Measurement public page",
                "url": "/measurement-test",
                "website_id": cls.website.id,
                "view_id": cls.view.id,
                "is_published": True,
            }
        )

    def _configuration(
        self,
        *,
        host="measurement.example.test",
        scheme="https",
        path="/measurement-test",
        internal=False,
        website=None,
    ):
        public_env = self.website.with_user(self.website.user_id).env
        mocked = SimpleNamespace(
            env=self.env if internal else public_env,
            website=website or self.website,
            httprequest=SimpleNamespace(
                scheme=scheme, host_url=f"{scheme}://{host}/", path=path
            ),
        )
        with patch.object(website_module, "request", mocked):
            return self.website._marketing_measurement_config()

    def test_capture_configuration_without_google_or_crm_action(self):
        self.website.google_analytics_key = False
        config = self._configuration()
        self.assertEqual(config["ga4"], "")
        self.assertEqual(config["form"], "")
        self.assertEqual(config["website_id"], self.website.id)
        self.assertNotIn("cookie_notice", config)
        self.assertNotIn("cookie_proceed_label", config)

    def test_request_boundary_for_public_configuration(self):
        for kwargs in (
            {"host": "legacy.example.test"},
            {"scheme": "http"},
            {"internal": True},
            {"path": "/contactus-thank-you"},
            {"path": "/my/orders"},
            {"website": self.env.ref("website.default_website")},
        ):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self._configuration(**kwargs), {})
        self.page.is_published = False
        self.assertEqual(self._configuration(), {})
        self.page.write({"is_published": True, "visibility": "password"})
        self.assertEqual(self._configuration(), {})

    def test_paused_binding_does_not_restore_native_google_loader(self):
        self.assertTrue(self.website._marketing_measurement_managed())
        self.binding.active = False
        self.assertTrue(self.website._marketing_measurement_managed())
        self.assertEqual(self._configuration(), {})
        other = self.env["website"].create({"name": "Native Google site"})
        self.assertFalse(other._marketing_measurement_managed())

    def test_whatsapp_matching_does_not_publish_admin_destination(self):
        action = self.env["marketing.website.action"].create(
            {
                "name": "Configured WhatsApp",
                "binding_id": self.binding.id,
                "kind": "whatsapp_handoff",
                "route_ref": "measurement.whatsapp",
                "source_path": self.page.url,
                "whatsapp_destination": "5519999999999",
                "whatsapp_message": "Mensagem pública específica",
                "fallback_path": self.page.url,
            }
        )
        config = self._configuration()
        expected = hashlib.sha256(
            (action.whatsapp_destination + "\n" + action.whatsapp_message).encode()
        ).hexdigest()
        self.assertEqual(config["whatsapp_link_hash"], expected)
        self.assertEqual(config["whatsapp"], action.public_ref)
        self.assertNotIn(action.whatsapp_destination, json.dumps(config))
        self.assertNotIn(
            action.whatsapp_message, json.dumps(config, ensure_ascii=False)
        )

    def test_stock_choices_for_both_backend_policies_and_paused_capture(self):
        for policy in ("informational_notice", "individual_consent"):
            values = {"website_tracking_policy": policy}
            if policy == "informational_notice":
                values["privacy_legal_basis_code"] = False
            else:
                values["privacy_legal_basis_code"] = "consent"
            self.endpoint.write(values)
            for paused in (False, True):
                self.binding.active = not paused
                view = self.env.ref("website.cookies_bar").with_context(
                    website_id=self.website.id
                )
                bar = view._get_combined_arch().xpath(
                    "//div[@id='website_cookies_bar']"
                )[0]
                html = self.env["ir.qweb"]._render(
                    copy.deepcopy(bar), {"website": self.website}, minimal_qcontext=True
                )
                root = etree.HTML(str(html))
                for ident in ("cookies-consent-all", "cookies-consent-essential"):
                    self.assertEqual(len(root.xpath("//*[@id='%s']" % ident)), 1)
                self.assertEqual(
                    root.xpath(
                        "//a[contains(@class,'o_cookies_bar_text_policy')]/@href"
                    ),
                    ["/cookie-policy"],
                )
                self.assertNotIn("marketing-cookie-notice", str(html))
                self.assertNotIn("data-marketing-tracking-notice", str(html))
        for name in (
            "marketing_cookie_notice_text",
            "marketing_cookie_proceed_label",
            "marketing_cookie_policy_url",
        ):
            self.assertNotIn(name, self.website._fields)
        self.assertFalse(
            self.env.ref(
                "marketing_center_website.cookie_notice", raise_if_not_found=False
            )
        )

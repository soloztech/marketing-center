import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from lxml import etree

from odoo.exceptions import ValidationError
from odoo.tests.common import SavepointCase

from ..models import website as website_module


class TestMarketingWebsiteMeasurementConfig(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.website = cls.env["website"].create({
            "name": "Generic measurement site", "domain": "https://measurement.example.test",
            "company_id": cls.env.company.id, "cookies_bar": True,
        })
        cls.endpoint = cls.env["marketing.web.ingress.endpoint"].create({
            "name": "Measurement configuration endpoint", "company_id": cls.env.company.id,
            "allowed_origins": cls.website.domain, "allowed_hosts": "measurement.example.test",
            "capture_enabled": True, "capture_purpose": "website_attribution",
            "privacy_policy_version": "test-v1", "privacy_notice_version": "test-v1",
            "privacy_legal_basis_code": "consent", "privacy_policy_justification": "Synthetic test",
            "identifier_retention_days": 30,
        })
        cls.binding = cls.env["marketing.website.ingress.binding"].create({
            "website_id": cls.website.id, "endpoint_id": cls.endpoint.id,
        })
        cls.view = cls.env["ir.ui.view"].create({
            "name": "Measurement public page", "type": "qweb",
            "key": "marketing_center_website.test_measurement_public_page",
            "arch_db": '<t t-name="marketing_center_website.test_measurement_public_page"><div>Public</div></t>',
        })
        cls.page = cls.env["website.page"].create({
            "name": "Measurement public page", "url": "/measurement-test",
            "website_id": cls.website.id, "view_id": cls.view.id, "is_published": True,
        })

    def _configuration(self, *, host="measurement.example.test", scheme="https", path="/measurement-test", internal=False, website=None):
        public_env = self.website.with_user(self.website.user_id).env
        mocked = SimpleNamespace(
            env=self.env if internal else public_env,
            website=website or self.website,
            httprequest=SimpleNamespace(scheme=scheme, host_url=f"{scheme}://{host}/", path=path),
        )
        with patch.object(website_module, "request", mocked):
            return self.website._marketing_measurement_config()

    def test_notice_configuration_without_google_or_crm_action(self):
        self.website.google_analytics_key = False
        config = self._configuration()
        self.assertEqual(config["ga4"], "")
        self.assertEqual(config["form"], "")
        self.assertEqual(config["website_id"], self.website.id)
        self.assertEqual(config["cookie_notice"], self.website.marketing_cookie_notice_text)
        self.assertTrue(config["cookie_proceed_label"])

    def test_request_boundary_for_public_configuration(self):
        for kwargs in (
            {"host": "legacy.example.test"}, {"scheme": "http"}, {"internal": True},
            {"path": "/contactus-thank-you"}, {"path": "/my/orders"},
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

    def test_settings_are_scoped_and_do_not_change_capture_policy(self):
        other = self.env["website"].create({"name": "Other notice"})
        before = (
            self.endpoint.config_revision,
            self.env["marketing.website.consent"].search_count([]),
            self.env["marketing.web.ingress.event"].search_count([]),
        )
        settings = self.env["res.config.settings"].create({"website_id": self.website.id})
        settings.write({
            "marketing_cookie_notice_text": "Texto atualizado pelo administrador.",
            "marketing_cookie_proceed_label": "Continuar",
            "marketing_cookie_policy_url": "/privacy-information",
        })
        self.assertEqual(self.website.marketing_cookie_proceed_label, "Continuar")
        self.assertNotEqual(other.marketing_cookie_proceed_label, "Continuar")
        self.assertEqual(self._configuration()["cookie_notice"], "Texto atualizado pelo administrador.")
        self.assertEqual(before, (
            self.endpoint.config_revision,
            self.env["marketing.website.consent"].search_count([]),
            self.env["marketing.web.ingress.event"].search_count([]),
        ))

    def test_whatsapp_matching_does_not_publish_admin_destination(self):
        action = self.env["marketing.website.action"].create({
            "name": "Configured WhatsApp", "binding_id": self.binding.id,
            "kind": "whatsapp_handoff", "route_ref": "measurement.whatsapp",
            "source_path": self.page.url, "whatsapp_destination": "5519999999999",
            "whatsapp_message": "Mensagem pública específica", "fallback_path": self.page.url,
        })
        config = self._configuration()
        expected = hashlib.sha256((action.whatsapp_destination + "\n" + action.whatsapp_message).encode()).hexdigest()
        self.assertEqual(config["whatsapp_link_hash"], expected)
        self.assertEqual(config["whatsapp"], action.public_ref)
        self.assertNotIn(action.whatsapp_destination, json.dumps(config))
        self.assertNotIn(action.whatsapp_message, json.dumps(config, ensure_ascii=False))

    def test_policy_link_rejects_executable_or_ambiguous_targets(self):
        for url in ("javascript:alert(1)", "//outside.example.test", "/\\outside.example.test", "http://example.test", "https://[invalid", "https://user:pass@example.test"):
            with self.subTest(url=url), self.assertRaises(ValidationError), self.env.cr.savepoint():
                self.website.marketing_cookie_policy_url = url
        for url in ("/cookie-policy", "/privacy#cookies", "https://privacy.example.test/policy"):
            self.website.marketing_cookie_policy_url = url
            self.assertEqual(self.website.marketing_cookie_policy_url, url)

    def test_informational_notice_renders_before_bootstrap_and_keeps_cms_anchors(self):
        self.endpoint.write({
            "website_tracking_policy": "informational_notice",
            "privacy_legal_basis_code": False,
        })
        # An inherited CMS view can still target the legacy XML anchors. They
        # must resolve during upgrades but never become HTML under this policy.
        self.env["ir.ui.view"].create({
            "name": "Synthetic CMS cookie style",
            "type": "qweb", "inherit_id": self.env.ref("marketing_center_website.cookie_notice").id,
            "arch_db": '<data><xpath expr="//a[@id=\'cookies-consent-all\']" position="attributes">'
                       '<attribute name="class">synthetic-cookie-style</attribute></xpath></data>',
        })
        view = self.env.ref("marketing_center_website.cookie_notice").with_context(website_id=self.website.id)
        arch = view._get_combined_arch()
        bar = arch.xpath("//div[@id='website_cookies_bar']")[0]
        self.assertEqual(len(bar.xpath(".//*[@id='cookies-consent-all']")), 1)
        self.assertEqual(len(bar.xpath(".//*[@id='cookies-consent-essential']")), 1)

        def rendered():
            html = self.env["ir.qweb"]._render(copy.deepcopy(bar), {"website": self.website}, minimal_qcontext=True)
            return etree.HTML(str(html))

        for paused in (False, True):
            if paused:
                self.binding.active = False
                self.endpoint.capture_enabled = False
            result = rendered()
            self.assertFalse(result.xpath("//*[@id='cookies-consent-essential' or @id='cookies-consent-all']"))
            self.assertEqual(len(result.xpath("//button[contains(@class,'marketing-cookie-notice-proceed')]")), 1)
            self.assertEqual(result.xpath("//div[@id='website_cookies_bar']/@data-marketing-tracking-notice"), ["1"])
            self.assertTrue(self.website._marketing_informational_notice())
        self.assertEqual(self._configuration(), {}, "paused capture cannot expose measurement")

    def test_single_notice_settings_and_dismissal_scope(self):
        self.assertNotIn("marketing_cookie_test_notice_text", self.website._fields)
        self.assertNotIn("marketing_cookie_test_notice_text", self.env["res.config.settings"]._fields)
        other = self.env["website"].create({"name": "Other notice dismissal"})
        key = self.website._marketing_notice_storage_key()
        self.assertNotEqual(other._marketing_notice_storage_key(), key)
        self.website.marketing_cookie_notice_text = "Novo aviso informativo."
        self.assertNotEqual(self.website._marketing_notice_storage_key(), key)

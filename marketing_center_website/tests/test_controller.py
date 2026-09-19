from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase
from odoo.tools import config


@tagged("-at_install", "post_install")
class TestMarketingWebsiteIngressController(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.website = cls.env.ref("website.default_website")
        cls.website.domain = "https://www.example.test"
        cls.endpoint = cls.env["marketing.web.ingress.endpoint"].create(
            {
                "capture_enabled": True,
                "capture_purpose": "web_attribution",
                "privacy_policy_version": "test-v1",
                "privacy_notice_version": "test-v1",
                "privacy_legal_basis_code": "documented_test_basis",
                "privacy_policy_justification": "Synthetic test policy.",
                "identifier_retention_days": 30,
                "name": "Website public config endpoint",
                "company_id": cls.website.company_id.id,
                "allowed_origins": "https://www.example.test",
                "allowed_hosts": "www.example.test",
            }
        )
        cls.binding = cls.env["marketing.website.ingress.binding"].search(
            [("website_id", "=", cls.website.id)], limit=1
        )
        if cls.binding:
            cls.binding.write(
                {
                    "endpoint_id": cls.endpoint.id,
                    "active": True,
                    "capture_mode": "legacy",
                }
            )
        else:
            cls.binding = cls.env["marketing.website.ingress.binding"].create(
                {
                    "website_id": cls.website.id,
                    "endpoint_id": cls.endpoint.id,
                    "capture_mode": "legacy",
                }
            )
        cls.authenticated_login = "website-ingress-authenticated"
        cls.authenticated_password = "website-ingress-authenticated"
        cls.env["res.users"].with_context(no_reset_password=True).create(
            {
                "name": "Website ingress authenticated visitor",
                "login": cls.authenticated_login,
                "password": cls.authenticated_password,
                "email": "website-ingress-authenticated@example.invalid",
                "company_id": cls.website.company_id.id,
                "company_ids": [(6, 0, [cls.website.company_id.id])],
                "groups_id": [(6, 0, [cls.env.ref("base.group_user").id])],
            }
        )

    def setUp(self):
        super().setUp()
        proxy = patch.dict(config.options, {"proxy_mode": True})
        proxy.start()
        self.addCleanup(proxy.stop)

    def _open_config(self):
        return self.opener.get(
            self.base_url() + "/marketing/website-ingress/config",
            headers={
                "X-Forwarded-Host": "www.example.test",
                "X-Forwarded-Proto": "https",
            },
        )

    def test_same_origin_config_is_minimal_and_side_effect_free(self):
        event_model = self.env["marketing.web.ingress.event"].sudo()
        touchpoint_model = self.env["marketing.attribution.touchpoint"].sudo()
        before = (event_model.search_count([]), touchpoint_model.search_count([]))
        response = self._open_config()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "enabled": True,
                "capture_mode": "legacy",
                "informational_notice": False,
                "capture_allowed": True,
                "ingest_path": "/marketing/web-ingress/%s" % self.endpoint.public_ref,
                "public_key": self.endpoint.public_key,
                "config_revision": self.endpoint.config_revision,
            },
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        # The Website dispatcher may set Odoo's non-authentication language
        # preference on the first anonymous request.  The configuration route
        # itself must never create an authenticated/session cookie.
        set_cookie = response.headers.get("Set-Cookie", "")
        self.assertNotIn("session_id=", set_cookie.lower())
        if set_cookie:
            self.assertTrue(set_cookie.startswith("frontend_lang="), set_cookie)
        self.env.invalidate_all()
        self.assertEqual(
            (event_model.search_count([]), touchpoint_model.search_count([])), before
        )

    def test_disabled_binding_returns_no_endpoint_material(self):
        self.binding.write({"active": False})
        response = self._open_config()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"enabled": False})
        self.assertNotIn(self.endpoint.public_ref, response.text)
        self.assertNotIn(self.endpoint.public_key, response.text)

    def test_disabled_capture_policy_returns_no_endpoint_material(self):
        self.endpoint.write({"capture_enabled": False})
        response = self._open_config()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"enabled": False})
        self.assertNotIn(self.endpoint.public_key, response.text)

    def test_authenticated_session_receives_no_public_endpoint_material(self):
        self.authenticate(self.authenticated_login, self.authenticated_password)
        response = self._open_config()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"enabled": False})
        self.assertNotIn(self.endpoint.public_ref, response.text)
        self.assertNotIn(self.endpoint.public_key, response.text)

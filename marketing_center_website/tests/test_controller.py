from odoo.tests import tagged
from odoo.tests.common import HttpCase


@tagged("-at_install", "post_install")
class TestMarketingWebsiteIngressController(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.website = cls.env.ref("website.default_website")
        cls.endpoint = cls.env["marketing.web.ingress.endpoint"].create(
            {
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
            cls.binding.write({"endpoint_id": cls.endpoint.id, "active": True})
        else:
            cls.binding = cls.env["marketing.website.ingress.binding"].create(
                {
                    "website_id": cls.website.id,
                    "endpoint_id": cls.endpoint.id,
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

    def test_same_origin_config_is_minimal_and_side_effect_free(self):
        event_model = self.env["marketing.web.ingress.event"].sudo()
        touchpoint_model = self.env["marketing.attribution.touchpoint"].sudo()
        before = (event_model.search_count([]), touchpoint_model.search_count([]))
        response = self.url_open("/marketing/website-ingress/config")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "enabled": True,
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
        response = self.url_open("/marketing/website-ingress/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"enabled": False})
        self.assertNotIn(self.endpoint.public_ref, response.text)
        self.assertNotIn(self.endpoint.public_key, response.text)

    def test_authenticated_session_receives_no_public_endpoint_material(self):
        self.authenticate(self.authenticated_login, self.authenticated_password)
        response = self.url_open("/marketing/website-ingress/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"enabled": False})
        self.assertNotIn(self.endpoint.public_ref, response.text)
        self.assertNotIn(self.endpoint.public_key, response.text)

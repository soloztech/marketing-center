from contextlib import contextmanager
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest import SkipTest
from unittest.mock import patch

from psycopg2.errors import SerializationFailure

from odoo import http
from odoo.tests.common import SavepointCase

from odoo.addons.utm.models.ir_http import IrHttp as NativeUtmHttp

from ..models import ir_http as policy


class TestNativeUtmPolicy(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if "crm.lead" not in cls.env:
            raise SkipTest("CRM form integration requires the optional CRM addon")
        cls.website = cls.env["website"].create({
            "name": "Native UTM policy test", "domain": "https://native-utm.example.test",
            "company_id": cls.env.company.id, "cookies_bar": True,
        })
        cls.endpoint = cls.env["marketing.web.ingress.endpoint"].create({
            "name": "Native UTM policy endpoint", "company_id": cls.env.company.id,
            "allowed_origins": cls.website.domain, "allowed_hosts": "native-utm.example.test",
            "website_tracking_policy": "informational_notice",
            "capture_enabled": True, "capture_purpose": "website_attribution",
            "privacy_policy_version": "test-v1", "privacy_notice_version": "test-v1",
            "privacy_legal_basis_code": False, "privacy_policy_justification": "Synthetic policy test",
            "identifier_retention_days": 30,
        })
        cls.binding = cls.env["marketing.website.ingress.binding"].create({
            "website_id": cls.website.id, "endpoint_id": cls.endpoint.id,
        })
        cls.view = cls.env["ir.ui.view"].create({
            "name": "Native UTM policy page", "type": "qweb",
            "key": "marketing_center_website.test_native_utm_policy",
            "arch_db": '<t t-name="marketing_center_website.test_native_utm_policy"><div>Test</div></t>',
        })
        cls.page = cls.env["website.page"].create({
            "name": "Native UTM policy page", "url": "/native-utm-policy-test",
            "website_id": cls.website.id, "view_id": cls.view.id, "is_published": True,
        })
        crm_model = cls.env["ir.model"]._get("crm.lead")
        crm_model.website_form_access = True
        cls.action = cls.env["marketing.website.action"].create({
            "name": "Native UTM policy CRM form", "binding_id": cls.binding.id,
            "kind": "form_submission", "route_ref": "test.native.utm.form",
            "source_path": cls.page.url, "form_model_id": crm_model.id,
        })
        cls.params = {"utm_campaign": "Native policy campaign", "utm_source": "Native policy source",
                      "utm_medium": "Native policy medium"}

    @contextmanager
    def _request(self, *, cookies=None, internal=False, path=None, host="native-utm.example.test",
                 scheme="https", method="GET", frontend=True, params=None):
        user = self.env.user if internal else self.website.user_id
        website = self.website.with_user(user).with_context(website_id=self.website.id)
        fake = SimpleNamespace(
            env=website.env, db=self.env.cr.dbname, website=website, is_frontend=frontend,
            session={},
            future_response=http.FutureResponse(), params=dict(self.params if params is None else params),
            httprequest=SimpleNamespace(host=host, host_url=f"{scheme}://{host}/", scheme=scheme,
                                        path=path or self.page.url, method=method, cookies=cookies or {}),
        )
        http._request_stack.push(fake)
        try:
            yield fake
        finally:
            http._request_stack.pop()

    def _write(self):
        response = http.Response("Test", content_type="text/html")
        self.env["ir.http"]._set_utm(response)
        cookies = SimpleCookie()
        for header in response.headers.getlist("Set-Cookie"):
            cookies.load(header)
        return cookies

    def _assert_native_values(self, cookies, *, allowed):
        self.assertEqual(set(cookies), {row[2] for row in policy._NATIVE_UTM_FIELDS})
        for parameter, _, name in policy._NATIVE_UTM_FIELDS:
            self.assertEqual(cookies[name].value, self.params[parameter])
            self.assertEqual(cookies[name]["max-age"], str(31 * 24 * 3600) if allowed else "0")
            self.assertEqual(cookies[name]["domain"], "native-utm.example.test")

    def test_native_writer_preserves_values_lifetime_and_other_cookie_policy(self):
        before = (self.endpoint.config_revision,
                  self.env["marketing.website.consent"].search_count([]))
        for preference in (None, '{}', '{"required":true}'):
            with self.subTest(preference=preference), self._request(
                cookies={} if preference is None else {"website_cookies_bar": preference}
            ) as fake:
                self.assertFalse(self.env["ir.http"]._is_allowed_cookie("optional"))
                self._assert_native_values(self._write(), allowed=True)
                self.assertFalse(hasattr(fake, policy._SCOPE_ATTRIBUTE))
                self.assertFalse(self.env["ir.http"]._is_allowed_cookie("optional"))
                unrelated = http.Response("Test")
                unrelated.set_cookie("unrelated_optional", "value", cookie_type="optional")
                self.assertIn("Max-Age=0", unrelated.headers["Set-Cookie"])
                self.assertNotIn("website_cookies_bar", fake.future_response.headers.get("Set-Cookie", ""))
        self.assertEqual(before, (self.endpoint.config_revision,
                                 self.env["marketing.website.consent"].search_count([])))

    def test_existing_preferences_use_native_behavior(self):
        for preference, allowed in (("false", False), ("true", False), ('"true"', False),
                                    ('{"optional":false}', False), ('{"optional":true}', True),
                                    ('{"optional":null}', False)):
            with self.subTest(preference=preference), self._request(
                cookies={"website_cookies_bar": preference}
            ) as fake:
                self.assertFalse(self.env["ir.http"]._marketing_allows_native_utm(http.Response("Test")))
                self._assert_native_values(self._write(), allowed=allowed)
                self.assertFalse(hasattr(fake, policy._SCOPE_ATTRIBUTE))
        # Invalid JSON belongs to Odoo's existing parser. Never turn it into permission.
        with self._request(cookies={"website_cookies_bar": "{invalid"}):
            self.assertFalse(self.env["ir.http"]._marketing_allows_native_utm(http.Response("Test")))

    def test_request_and_response_boundaries(self):
        for kwargs in ({"internal": True}, {"path": "/web/login"}, {"path": "/missing-page"},
                       {"host": "unconfigured.example.test"}, {"scheme": "http"},
                       {"method": "POST"}, {"frontend": False}, {"params": {}}):
            with self.subTest(kwargs=kwargs), self._request(**kwargs):
                self.assertFalse(self.env["ir.http"]._marketing_allows_native_utm(http.Response("Test")))
        with self._request():
            for status in (201, 204, 301, 403, 404, 500):
                self.assertFalse(self.env["ir.http"]._marketing_allows_native_utm(http.Response("Test", status=status)))
            self.assertFalse(self.env["ir.http"]._marketing_allows_native_utm(
                http.Response("{}", status=200, content_type="application/json")
            ))

    def test_configuration_page_and_action_guards(self):
        for record, field, bad_value in ((self.page, "is_published", False),
                                         (self.page, "visibility", "password"),
                                         (self.action, "active", False),
                                         (self.binding, "active", False),
                                         (self.endpoint, "capture_enabled", False)):
            before = record[field]
            record[field] = bad_value
            with self.subTest(field=field, model=record._name), self._request():
                self._assert_native_values(self._write(), allowed=False)
            record[field] = before
        self.endpoint.write({"website_tracking_policy": "individual_consent", "privacy_legal_basis_code": "consent"})
        with self._request():
            self._assert_native_values(self._write(), allowed=False)

    def test_ambiguous_form_does_not_authorize_native_cookie_exception(self):
        self.env["marketing.website.action"].create({
            "name": "Second active form", "binding_id": self.binding.id,
            "kind": "form_submission", "route_ref": "test.native.utm.second",
            "source_path": self.page.url, "form_model_id": self.action.form_model_id.id,
        })
        with self._request():
            self._assert_native_values(self._write(), allowed=False)

    def test_extended_tracking_fields_fall_back_to_native_cookie_policy(self):
        native = list(policy._NATIVE_UTM_FIELDS)
        for fields in (native + [("utm_extra", "extra_id", "odoo_utm_extra")], list(reversed(native))):
            with self.subTest(fields=fields), self._request(), patch.object(
                type(self.env["utm.mixin"]), "tracking_fields", return_value=fields
            ):
                self._assert_native_values(self._write(), allowed=False)

    def test_native_writer_exception_restores_previous_request_scope(self):
        for previous in (policy._UNSET, object()):
            with self._request() as fake:
                if previous is not policy._UNSET:
                    setattr(fake, policy._SCOPE_ATTRIBUTE, previous)
                with patch.object(NativeUtmHttp, "_set_utm", side_effect=RuntimeError("Native writer failure")), self.assertRaises(RuntimeError):
                    self._write()
                if previous is policy._UNSET:
                    self.assertFalse(hasattr(fake, policy._SCOPE_ATTRIBUTE))
                else:
                    self.assertIs(getattr(fake, policy._SCOPE_ATTRIBUTE), previous)
                self.assertFalse(self.env["ir.http"]._is_allowed_cookie("optional"))

    def test_marketing_failure_falls_back_but_database_retry_is_preserved(self):
        with self._request(), patch.object(
            type(self.website), "_marketing_measurement_config", side_effect=ValueError("Bad configuration")
        ):
            self._assert_native_values(self._write(), allowed=False)
        with self._request() as fake, patch.object(
            type(self.website), "_marketing_measurement_config", side_effect=SerializationFailure("Retry request")
        ), self.assertRaises(SerializationFailure):
            self._write()
        self.assertFalse(hasattr(fake, policy._SCOPE_ATTRIBUTE))

    def test_unchanged_utm_cookie_values_are_not_reissued(self):
        existing = {cookie: self.params[parameter] for parameter, _, cookie in policy._NATIVE_UTM_FIELDS}
        with self._request(cookies=existing):
            self.assertFalse(self._write())

import datetime
import json
import uuid

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from ..models.consent import CONSENT_CONTEXT_TOKEN


class TestIndividualWebsiteConsent(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.website = cls.env.ref("website.default_website")
        cls.website.write({"cookies_bar": True})
        cls.endpoint = cls.env["marketing.web.ingress.endpoint"].create(
            {
                "name": "Synthetic consent endpoint",
                "company_id": cls.website.company_id.id,
                "allowed_origins": "https://example.test",
                "allowed_hosts": "example.test",
                "capture_enabled": True,
                "capture_purpose": "website_attribution",
                "privacy_policy_version": "test-v1",
                "privacy_notice_version": "test-v1",
                "privacy_legal_basis_code": "consent",
                "privacy_policy_justification": "Synthetic explicit Website choice",
                "identifier_retention_days": 90,
                "consent_ttl_days": 90,
            }
        )
        cls.binding = cls.env["marketing.website.ingress.binding"].search(
            [("website_id", "=", cls.website.id)], limit=1
        )
        if cls.binding:
            cls.binding.write({"endpoint_id": cls.endpoint.id, "active": True})
        else:
            cls.binding = cls.env["marketing.website.ingress.binding"].create(
                {"website_id": cls.website.id, "endpoint_id": cls.endpoint.id}
            )
        cls.consent = cls.env["marketing.website.consent"]

    def _granted(self):
        return self.consent._decide(self.binding, True)

    def _trusted_endpoint(self, decision):
        return self.endpoint.with_context(
            website_consent_internal=CONSENT_CONTEXT_TOKEN,
            website_consent_id=decision.id,
        )

    def test_policy_is_not_an_individual_grant(self):
        self.assertTrue(self.endpoint._privacy_policy_configured())
        self.assertFalse(self.endpoint._capture_policy_allows())
        decision = self._granted()
        forged = self.endpoint.with_context(
            website_consent_internal=True, website_consent_id=decision.id
        )
        self.assertFalse(forged._capture_policy_allows())
        self.assertTrue(self._trusted_endpoint(decision)._capture_policy_allows())

    def test_cookie_is_signed_scoped_and_revocable(self):
        decision = self._granted()
        cookie = decision._cookie()
        self.assertEqual(self.consent._from_cookie(self.endpoint, cookie), decision)
        self.assertFalse(
            self.consent._from_cookie(
                self.endpoint, cookie[:-1] + ("0" if cookie[-1] != "0" else "1")
            )
        )
        other = self.env["marketing.web.ingress.endpoint"].create(
            {
                "name": "Other endpoint",
                "allowed_origins": "https://other.example.test",
                "allowed_hosts": "other.example.test",
            }
        )
        self.assertFalse(self.consent._from_cookie(other, cookie))
        decision.with_context(website_consent_internal=CONSENT_CONTEXT_TOKEN).write(
            {"revoked_at": fields.Datetime.now()}
        )
        self.assertFalse(self._trusted_endpoint(decision)._capture_policy_allows())
        with self.assertRaises(AccessError):
            self.consent._decide(self.binding, True, cookie)

    def test_old_policy_and_expired_decision_cannot_capture(self):
        decision = self._granted()
        self.endpoint.write({"privacy_notice_version": "test-v2"})
        self.assertFalse(self._trusted_endpoint(decision)._capture_policy_allows())
        expired = self.consent._decide(
            self.binding, True, now=fields.Datetime.now() - datetime.timedelta(days=91)
        )
        self.assertFalse(self._trusted_endpoint(expired)._capture_policy_allows())
        self.consent._gc_expired_decision_identifiers()
        self.assertFalse(expired.public_ref)
        self.assertTrue(expired.identifier_erased_at)

    def test_decision_is_not_editable_even_by_rpc_administrator(self):
        decision = self._granted()
        with self.assertRaises(AccessError):
            decision.write({"granted": False})
        with self.assertRaises(AccessError):
            decision.unlink()
        with self.assertRaises(AccessError):
            self.consent.create({"public_ref": "not a trusted decision"})

    def test_manual_retention_is_explicit_and_separate_from_consent_validity(self):
        self.endpoint.write(
            {"retention_mode": "manual", "identifier_retention_days": 0}
        )
        self.assertTrue(self.endpoint._privacy_policy_configured())
        self.assertFalse(self.endpoint._retention_deadline(fields.Datetime.now()))
        decision = self._granted()
        self.assertEqual((decision.expires_at - decision.decided_at).days, 90)
        self.assertTrue(self._trusted_endpoint(decision)._capture_policy_allows())

    def _informational_policy(self):
        return self.endpoint._activate_informational_notice(
            website_id=self.website.id, binding_id=self.binding.id,
            company_id=self.website.company_id.id,
            expected_revision=self.endpoint.config_revision,
            policy_version="informational-v2", notice_version="notice-v2",
            justification="Synthetic operator decision; no visitor grant.",
        )

    def _test_entry(self, endpoint=None, *, path="/landing", origin="https://example.test"):
        endpoint = endpoint if endpoint is not None else self.endpoint
        return self.env["marketing.web.ingress.service"].with_env(endpoint.env)._ingest_payload(
            endpoint,
            {
                "event_id": str(uuid.uuid4()),
                "event_type": "entry_point",
                "occurred_at": fields.Datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "landing_url": "https://example.test" + path,
                "utm_source": "synthetic-source",
                "utm_medium": "cpc",
                "utm_campaign": "synthetic-informational-notice",
                "session_ref": str(uuid.uuid4()),
            },
            origin=origin,
            body_size_bytes=512,
            ingress_provenance="browser_capability",
        )

    def test_operator_policy_is_explicit_and_default_ignores_old_parameter(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "marketing_center_website.tracking_test_endpoint_ids", json.dumps([self.endpoint.id])
        )
        self.assertEqual(self.endpoint.website_tracking_policy, "individual_consent")
        self.assertFalse(self.endpoint._capture_policy_allows())
        revision = self.endpoint.config_revision
        result = self._informational_policy()
        self.assertEqual(result["config_revision"], revision + 1)
        self.assertEqual(self.endpoint.privacy_policy_set_by, self.env.user)
        self.assertTrue(self.endpoint._capture_policy_allows())
        self.assertFalse(self.endpoint.privacy_legal_basis_code)
        other = self.env["marketing.web.ingress.endpoint"].create({
            "name": "Unselected endpoint", "allowed_hosts": "other.test",
            "allowed_origins": "https://other.test",
            "privacy_legal_basis_code": "consent",
            **{name: self.endpoint[name] for name in (
                "capture_enabled", "capture_purpose", "privacy_policy_version",
                "privacy_notice_version", "privacy_policy_justification",
                "identifier_retention_days",
            )},
        })
        self.assertFalse(other._capture_policy_allows())
        self.endpoint.capture_enabled = False
        self.assertFalse(self.endpoint._capture_policy_allows())

    def test_operator_migration_rejects_wrong_scope_or_stale_revision(self):
        values = dict(website_id=self.website.id, binding_id=self.binding.id,
                      company_id=self.website.company_id.id,
                      expected_revision=self.endpoint.config_revision,
                      policy_version="new-v2", notice_version="new-v2",
                      justification="Reviewed synthetic operator decision")
        for changed in ({"website_id": self.website.id + 1000},
                        {"company_id": self.website.company_id.id + 1000},
                        {"expected_revision": self.endpoint.config_revision + 1},
                        {"expected_revision": True}, {"justification": ""}):
            with self.subTest(changed=changed), self.assertRaises(ValidationError), self.env.cr.savepoint():
                self.endpoint._activate_informational_notice(**{**values, **changed})
        self.assertFalse(self.endpoint._capture_policy_allows())
        self._informational_policy()
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.endpoint.privacy_legal_basis_code = "consent"

    def test_informational_capture_never_relabels_old_receipts_or_invents_grant(self):
        decision = self._granted()
        before = decision.read()[0]
        count = self.consent.search_count([])
        self._informational_policy()
        # Odoo represents an empty Char as False; the DTO boundary must accept
        # the absence of a legal-basis assertion without inventing one.
        self.assertIs(self.endpoint.privacy_legal_basis_code, False)
        result = self._test_entry(self._trusted_endpoint(decision))
        touchpoint = self.env["marketing.attribution.touchpoint"].browse(result.touchpoint_id)
        self.assertEqual(touchpoint.consent_state, "unknown")
        self.assertFalse(touchpoint.legal_basis_code)
        self.assertEqual(touchpoint.privacy_decision_source, "operator.website_notice")
        self.assertFalse(touchpoint.privacy_decided_at)
        self.assertEqual(touchpoint.extensions_json["web_ingress.website_tracking_policy"], "informational_notice")
        self.assertNotIn("web_ingress.tracking_test_mode", touchpoint.extensions_json)
        self.assertEqual(self.consent.search_count([]), count)
        self.assertEqual(decision.read()[0], before)
        self.endpoint.capture_enabled = False
        with self.assertRaises(AccessError):
            self._test_entry()
        self.assertEqual(touchpoint.consent_state, "unknown")

    def test_informational_policy_retains_origin_path_and_authentication_guards(self):
        from ..models import consent, consent_service
        from unittest.mock import patch
        self._informational_policy()
        with self.assertRaises(AccessError):
            self._test_entry(path="/web/login")
        with self.assertRaises(ValidationError):
            self._test_entry(origin="https://other.invalid")
        with self._http("/marketing/web-ingress/" + self.endpoint.public_ref, {}):
            fake = consent.request
            fake.session.uid = self.env.uid
            with patch.object(consent_service, "request", fake), self.assertRaises(AccessError):
                self._test_entry()
            fake.session.uid = False

    def test_informational_config_and_refusal_never_create_acceptance(self):
        self.website.domain = "https://example.test"
        self._informational_policy()
        before = self.consent.search_count([])
        with self._http("/marketing/website-consent/config") as controller:
            config = json.loads(controller.configuration().get_data())
            self.assertTrue(config["informational_notice"])
            self.assertTrue(config["capture_allowed"])
            self.assertFalse(config["available"])
            self.assertFalse(config["granted"])
            self.assertNotIn("tracking_test_mode", config)
        with self._http("/marketing/website-consent/config", base_url="https://legacy.invalid") as controller:
            config = json.loads(controller.configuration().get_data())
            self.assertFalse(config["capture_allowed"])
        with self._http("/marketing/website-consent/decision", {
            "granted": False, "config_revision": self.endpoint.config_revision,
            "policy_version": "informational-v2", "notice_version": "notice-v2",
        }) as controller:
            answer = json.loads(controller.decide().get_data())
            self.assertTrue(answer["accepted"])
            self.assertTrue(answer["informational_notice"])
            self.assertTrue(answer["capture_allowed"])
            self.assertFalse(answer["granted"])
        with self.assertRaises(AccessError):
            self._granted()
        self.assertEqual(self.consent.search_count([]), before)

    def test_internal_http_worker_requires_real_token_and_current_decision(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from werkzeug.test import EnvironBuilder
        from werkzeug.wrappers import Request
        from ..models import consent

        decision = self._granted()
        other = self.env["marketing.web.ingress.endpoint"].create(
            {
                "name": "Worker other endpoint",
                "allowed_origins": "https://other.test",
                "allowed_hosts": "other.test",
            }
        )
        worker = SimpleNamespace(
            httprequest=Request(
                EnvironBuilder(
                    path="/queue_job/runjob",
                    base_url="http://127.0.0.1:48069",
                    headers={"X-Marketing-Consent-Ref": decision.public_ref},
                ).get_environ()
            ),
            session=SimpleNamespace(uid=False),
        )
        trusted = self._trusted_endpoint(decision)
        with patch.object(consent, "request", worker):
            self.assertFalse(self.endpoint._capture_policy_allows())
            for forged in (True, "CONSENT_CONTEXT_TOKEN", {"trusted": True}):
                self.assertFalse(
                    self.endpoint.with_context(
                        website_consent_internal=forged,
                        website_consent_id=decision.id,
                    )._capture_policy_allows()
                )
            self.assertTrue(trusted._capture_policy_allows())
            internal = self.consent.with_env(trusted.env)
            self.assertFalse(internal._current(other))
            with patch.object(fields.Datetime, "now", return_value=decision.expires_at):
                self.assertFalse(trusted._capture_policy_allows())
            self.endpoint.write({"privacy_notice_version": "worker-v2"})
            self.assertFalse(trusted._capture_policy_allows())
            current = self._granted()
            self.assertTrue(self._trusted_endpoint(current)._capture_policy_allows())
            current.with_context(website_consent_internal=CONSENT_CONTEXT_TOKEN).write(
                {"revoked_at": fields.Datetime.now()}
            )
            self.assertFalse(self._trusted_endpoint(current)._capture_policy_allows())

    def _http(
        self, path, payload=None, *, cookie="", extra_headers=None,
        base_url="https://example.test",
    ):
        import contextlib
        import json
        from types import SimpleNamespace
        from unittest.mock import patch
        from werkzeug.test import EnvironBuilder
        from werkzeug.wrappers import Request
        from ..controllers import website_action, website_consent
        from ..models import consent

        @contextlib.contextmanager
        def mocked():
            headers = {
                "Origin": "https://example.test",
                "Sec-Fetch-Site": "same-origin",
                "X-Marketing-Consent": "1",
                "Cookie": cookie,
            }
            headers.update(extra_headers or {})
            builder = EnvironBuilder(
                path=path,
                base_url=base_url,
                method="POST" if payload is not None else "GET",
                data=json.dumps(payload) if payload is not None else None,
                content_type="application/json" if payload is not None else None,
                headers=headers,
            )
            fake = SimpleNamespace(
                httprequest=Request(builder.get_environ()),
                env=self.env(user=self.website.user_id.id),
                website=self.website,
                session=SimpleNamespace(uid=False),
            )
            with patch.object(website_consent, "request", fake), patch.object(
                website_action, "request", fake
            ), patch.object(consent, "request", fake):
                yield website_consent.MarketingWebsiteConsentController()

        return mocked()

    def test_individual_http_choice_scope_and_stale_request_revocation(self):
        import json
        from http.cookies import SimpleCookie
        from urllib.parse import quote

        self.website.write({"domain": "https://example.test"})
        decision_body = {
            "granted": True,
            "config_revision": self.endpoint.config_revision,
            "policy_version": "test-v1",
            "notice_version": "test-v1",
        }
        with self._http("/marketing/website-consent/config") as controller:
            response = controller.configuration()
            self.assertFalse(json.loads(response.get_data())["granted"])
            self.assertNotIn("Set-Cookie", response.headers)
        with self._http(
            "/marketing/website-consent/decision", decision_body
        ) as controller:
            self.assertEqual(controller.decide().status_code, 400)
        native = "website_cookies_bar=" + quote('{"required":true,"optional":true}')
        with self._http(
            "/marketing/website-consent/decision",
            decision_body,
            cookie=native,
            extra_headers={"Origin": "https://attacker.invalid"},
        ) as controller:
            self.assertFalse(json.loads(controller.decide().get_data())["accepted"])
        with self._http(
            "/marketing/website-consent/decision", decision_body, cookie=native
        ) as controller:
            response = controller.decide()
            self.assertTrue(json.loads(response.get_data())["accepted"])
            cookie = SimpleCookie()
            cookie.load(response.headers["Set-Cookie"])
            self.assertTrue(cookie["mc_website_consent"]["httponly"])
            self.assertTrue(cookie["mc_website_consent"]["secure"])
            self.assertEqual(cookie["mc_website_consent"]["samesite"], "Strict")
            raw = cookie["mc_website_consent"].value
        combined = native + "; mc_website_consent=" + raw
        decision = self.consent._from_cookie(self.endpoint, raw)
        with self._http(
            "/marketing/website-consent/config", cookie=combined
        ) as controller:
            self.assertTrue(
                json.loads(controller.configuration().get_data())["granted"]
            )
        with self._http(
            "/marketing/web-ingress/" + self.endpoint.public_ref,
            {},
            cookie=combined,
            extra_headers={"X-Marketing-Consent-Ref": decision.public_ref},
        ):
            self.assertTrue(self.endpoint._capture_policy_allows())
        with self._http(
            "/marketing/web-ingress/" + self.endpoint.public_ref, {}, cookie=combined
        ):
            self.assertFalse(self.endpoint._capture_policy_allows())
        with self._http(
            "/marketing/website-consent/decision",
            dict(decision_body, granted=False),
            cookie=combined,
        ) as controller:
            self.assertTrue(json.loads(controller.decide().get_data())["accepted"])
        self.assertTrue(decision.revoked_at)
        with self._http(
            "/marketing/web-ingress/" + self.endpoint.public_ref,
            {},
            cookie=combined,
            extra_headers={"X-Marketing-Consent-Ref": decision.public_ref},
        ):
            self.assertFalse(self.endpoint._capture_policy_allows())
        with self._http(
            "/marketing/website-consent/decision", decision_body, cookie=combined
        ) as controller:
            self.assertEqual(controller.decide().status_code, 400)

import json
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from odoo.addons.marketing_center_web_ingress.services.errors import (
    WebIngressSerializationFailure,
)


@tagged("-at_install", "post_install")
class TestMarketingWebsiteActionController(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.website = cls.env.ref("website.default_website")
        cls.origin = cls.base_url().rstrip("/")
        cls.host = urlsplit(cls.origin).hostname
        cls.endpoint = cls.env["marketing.web.ingress.endpoint"].create(
            {
                "capture_enabled": True,
                "capture_purpose": "web_attribution",
                "privacy_policy_version": "test-v1",
                "privacy_notice_version": "test-v1",
                "privacy_legal_basis_code": "documented_test_basis",
                "privacy_policy_justification": "Synthetic test policy.",
                "identifier_retention_days": 30,
                "name": "Website action HTTP endpoint",
                "company_id": cls.website.company_id.id,
                "allowed_origins": cls.origin,
                "allowed_hosts": cls.host,
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
        cls.form_model = cls.env["ir.model"]._get("res.partner")
        cls.form_model.write({"website_form_access": True})
        cls.form_action = cls.env["marketing.website.action"].create(
            {
                "name": "HTTP form",
                "binding_id": cls.binding.id,
                "kind": "form_submission",
                "route_ref": "form.http",
                "source_path": "/contactus",
                "form_model_id": cls.form_model.id,
            }
        )
        cls.whatsapp_action = cls.env["marketing.website.action"].create(
            {
                "name": "HTTP WhatsApp",
                "binding_id": cls.binding.id,
                "kind": "whatsapp_handoff",
                "route_ref": "whatsapp.http",
                "source_path": "/contactus",
                "whatsapp_destination": "5519999999999",
                "fallback_path": "/contactus",
            }
        )
        cls.authenticated_login = "website-action-authenticated"
        cls.authenticated_password = "website-action-authenticated"
        cls.env["res.users"].with_context(no_reset_password=True).create(
            {
                "name": "Website action authenticated visitor",
                "login": cls.authenticated_login,
                "password": cls.authenticated_password,
                "email": "website-action-authenticated@example.invalid",
                "company_id": cls.website.company_id.id,
                "company_ids": [(6, 0, [cls.website.company_id.id])],
                "groups_id": [(6, 0, [cls.env.ref("base.group_user").id])],
            }
        )

    def _post(self, path, payload):
        return self.opener.post(
            self.base_url() + path,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": self.origin,
                "Sec-Fetch-Site": "same-origin",
            },
        )

    def test_form_exchange_is_opaque_idempotent_and_rejects_pii_fields(self):
        claim = {
            "action_ref": self.form_action.public_ref,
            "event_id": "11111111-1111-4111-8111-111111111111",
            "session_ref": "22222222-2222-4222-8222-222222222222",
        }
        receipt = self.env["marketing.website.action.service"]._prepare_form_receipt(
            self.website,
            self.form_model.model,
            claim,
            self.origin,
        )
        response = self._post(
            "/marketing/website-action/form/exchange",
            {**claim, "receipt": receipt},
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json(), {"accepted": True})
        replay = self._post(
            "/marketing/website-action/form/exchange",
            {**claim, "receipt": receipt},
        )
        self.assertEqual(replay.status_code, 202, replay.text)
        rejected = self._post(
            "/marketing/website-action/form/exchange",
            {**claim, "receipt": receipt, "email": "private@example.invalid"},
        )
        self.assertEqual(rejected.status_code, 400)
        self.env.invalidate_all()
        self.assertEqual(
            self.env["marketing.web.ingress.event"]
            .sudo()
            .search_count([("endpoint_id", "=", self.endpoint.id)]),
            1,
        )

    def test_whatsapp_claim_returns_only_one_use_same_origin_redirect(self):
        response = self._post(
            "/marketing/website-action/whatsapp/claim",
            {
                "action_ref": self.whatsapp_action.public_ref,
                "event_id": "33333333-3333-4333-8333-333333333333",
                "session_ref": "44444444-4444-4444-8444-444444444444",
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertEqual(set(payload), {"accepted", "redirect_path"})
        self.assertNotIn("wa.me", response.text)
        first = self.url_open(payload["redirect_path"], allow_redirects=False)
        self.assertEqual(first.status_code, 303)
        self.assertEqual(first.headers["Location"], "https://wa.me/5519999999999")
        second = self.url_open(payload["redirect_path"], allow_redirects=False)
        self.assertEqual(second.status_code, 303)
        self.assertEqual(second.headers["Location"], "/contactus")

    def test_serialization_retry_replays_the_same_bounded_action_body(self):
        service_class = type(self.env["marketing.web.ingress.service"])
        original_ingest = service_class._ingest_payload
        attempts = []

        def fail_once(service, *args, **kwargs):
            attempts.append(
                {
                    "payload": json.dumps(
                        args[1], sort_keys=True, separators=(",", ":")
                    ),
                    "body_size_bytes": kwargs["body_size_bytes"],
                }
            )
            if len(attempts) == 1:
                raise WebIngressSerializationFailure("synthetic Website retry")
            return original_ingest(service, *args, **kwargs)

        with patch.object(service_class, "_ingest_payload", new=fail_once):
            response = self._post(
                "/marketing/website-action/whatsapp/claim",
                {
                    "action_ref": self.whatsapp_action.public_ref,
                    "event_id": "99999999-9999-4999-8999-999999999999",
                    "session_ref": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                },
            )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])

    def test_prefetch_and_cross_origin_claims_create_no_evidence(self):
        body = {
            "action_ref": self.whatsapp_action.public_ref,
            "event_id": "55555555-5555-4555-8555-555555555555",
            "session_ref": "66666666-6666-4666-8666-666666666666",
        }
        prefetch = self.opener.post(
            self.base_url() + "/marketing/website-action/whatsapp/claim",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": self.origin,
                "Purpose": "prefetch",
            },
        )
        self.assertEqual(prefetch.status_code, 404)
        cross_origin = self.opener.post(
            self.base_url() + "/marketing/website-action/whatsapp/claim",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": "https://evil.example",
            },
        )
        self.assertEqual(cross_origin.status_code, 404)
        self.env.invalidate_all()
        self.assertFalse(
            self.env["marketing.web.ingress.event"]
            .sudo()
            .search_count([("endpoint_id", "=", self.endpoint.id)])
        )

    def test_authenticated_claim_is_disabled_and_creates_no_evidence(self):
        self.authenticate(self.authenticated_login, self.authenticated_password)
        response = self._post(
            "/marketing/website-action/whatsapp/claim",
            {
                "action_ref": self.whatsapp_action.public_ref,
                "event_id": "77777777-7777-4777-8777-777777777777",
                "session_ref": "88888888-8888-4888-8888-888888888888",
            },
        )
        self.assertEqual(response.status_code, 404)
        self.env.invalidate_all()
        self.assertFalse(
            self.env["marketing.web.ingress.event"]
            .sudo()
            .search_count([("endpoint_id", "=", self.endpoint.id)])
        )

    def test_public_action_rate_limit_is_explicit_and_creates_no_evidence(self):
        admission_class = type(self.env["marketing.web.ingress.admission"])
        with patch.object(admission_class, "_admit", return_value=False):
            response = self._post(
                "/marketing/website-action/whatsapp/claim",
                {
                    "action_ref": self.whatsapp_action.public_ref,
                    "event_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    "session_ref": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                },
            )
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.headers["Retry-After"], "60")
        self.env.invalidate_all()
        self.assertFalse(
            self.env["marketing.web.ingress.event"]
            .sudo()
            .search_count([("endpoint_id", "=", self.endpoint.id)])
        )

    def test_invalid_action_claim_still_crosses_the_admission_safety_net(self):
        admission_class = type(self.env["marketing.web.ingress.admission"])
        original_admit = admission_class._admit
        calls = []

        def record_admission(admission, endpoint, request_kind, now=None):
            calls.append((endpoint.id, request_kind))
            return original_admit(
                admission,
                endpoint,
                request_kind,
                now=now,
            )

        with patch.object(admission_class, "_admit", new=record_admission):
            response = self._post(
                "/marketing/website-action/whatsapp/claim",
                {
                    "action_ref": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                    "event_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                    "session_ref": "ffffffff-ffff-4fff-8fff-ffffffffffff",
                },
            )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(calls, [(self.endpoint.id, "website_whatsapp")])

    def test_blocked_optional_capture_preserves_native_form_creation(self):
        self.endpoint.write({"capture_enabled": False})
        self.env["ir.model.fields"].formbuilder_whitelist("res.partner", ["name"])
        name = "Native contact without optional attribution"
        query = urlencode(
            {
                "mc_action": self.form_action.public_ref,
                "mc_event": "11111111-1111-4111-8111-111111111111",
                "mc_session": "22222222-2222-4222-8222-222222222222",
            }
        )
        response = self.opener.post(
            "%s/website/form/res.partner?%s" % (self.base_url(), query),
            data={"name": name},
            headers={"Origin": self.origin},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json().get("id"), response.text)
        self.assertNotIn("marketing_center_receipt", response.json())
        self.env.invalidate_all()
        self.assertTrue(self.env["res.partner"].search([("name", "=", name)]))
        self.assertFalse(
            self.env["marketing.web.ingress.event"]
            .sudo()
            .search(
                [
                    ("endpoint_id", "=", self.endpoint.id),
                ]
            )
        )

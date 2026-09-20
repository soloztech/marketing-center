"""Native HTTP capture, fixed routing, current policy and acquisition boundaries."""

import datetime
import json
import uuid
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit, urlunsplit

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import HttpCase
from odoo.tools import config

from ..models.website_action import account_whatsapp_digits


@tagged("-at_install", "post_install")
class TestNativeWebsiteWhatsAppHttp(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.website = cls.env.ref("website.default_website")
        local = urlsplit(cls.base_url())
        cls.proxy_host = local.netloc
        cls.origin = urlunsplit(("https", local.netloc, "", "", ""))
        cls.website.domain = cls.origin
        cls.endpoint = cls.env["marketing.web.ingress.endpoint"].create({
            "name": "Synthetic native WhatsApp HTTP", "company_id": cls.website.company_id.id,
            "allowed_origins": cls.origin, "allowed_hosts": local.hostname,
            "capture_enabled": True, "capture_purpose": "website_attribution",
            "website_tracking_policy": "informational_notice",
            "privacy_legal_basis_code": False, "privacy_policy_version": "handoff-http-v1",
            "privacy_notice_version": "handoff-http-v1",
            "privacy_policy_justification": "Synthetic HTTP regression fixture",
            "identifier_retention_days": 30,
        })
        cls.binding = cls.env["marketing.website.ingress.binding"].search([
            ("website_id", "=", cls.website.id),
        ], limit=1)
        if cls.binding:
            cls.binding.write({"endpoint_id": cls.endpoint.id, "active": True, "capture_mode": "native"})
        else:
            cls.binding = cls.env["marketing.website.ingress.binding"].create({
                "website_id": cls.website.id, "endpoint_id": cls.endpoint.id, "capture_mode": "native",
            })
        cls.page_path = "/whatsapp-handoff-http-test"
        view = cls.env["ir.ui.view"].create({
            "name": "Synthetic WhatsApp handoff page", "type": "qweb",
            "key": "marketing_center_website_whatsapp.http_page",
            "arch_db": '<t t-name="marketing_center_website_whatsapp.http_page"><div>Test</div></t>',
        })
        cls.page = cls.env["website.page"].create({
            "name": "Synthetic WhatsApp handoff page", "url": cls.page_path,
            "website_id": cls.website.id, "view_id": view.id, "is_published": True,
        })
        cls.account = cls.env["contact.center.account"].create({
            "name": "Synthetic WhatsApp destination", "company_id": cls.website.company_id.id,
            "platform": "whatsapp", "own_external_identity": "5519999999999:5@s.whatsapp.net",
        })
        cls.action = cls.env["marketing.website.action"].create({
            "name": "Synthetic native WhatsApp action", "binding_id": cls.binding.id,
            "kind": "whatsapp_handoff", "route_ref": "whatsapp.native.http",
            "source_path": cls.page_path, "whatsapp_destination": "5519999999999",
            "whatsapp_message": "Olá! Quero saber mais.", "handoff_enabled": True,
            "handoff_account_id": cls.account.id, "handoff_reference_prefix": "CP",
        })

    def setUp(self):
        super().setUp()
        proxy = patch.dict(config.options, {"proxy_mode": True})
        proxy.start()
        self.addCleanup(proxy.stop)
        self.event_id = str(uuid.uuid4())

    def _post(self, *, payload=None, headers=None, query=None):
        return self.opener.post(
            self.base_url() + "/marketing/website-whatsapp/claim",
            data=json.dumps(payload if payload is not None else {
                "action_ref": self.action.public_ref, "event_id": self.event_id,
            }).encode(),
            headers={
                "Content-Type": "application/json", "Origin": self.origin,
                "Referer": self.origin + self.page_path + "?" + (
                    query or "gad_campaignid=23172115632&gclid=synthetic-click"
                ),
                "X-Forwarded-Host": self.proxy_host, "X-Forwarded-Proto": "https",
                "Sec-Fetch-Site": "same-origin", **(headers or {}),
            }, allow_redirects=False,
        )

    def _clicks(self):
        self.env.invalidate_all()
        return self.env["marketing.website.whatsapp.handoff"].search([
            ("action_id", "=", self.action.id),
        ])

    def test_http_capture_returns_fixed_destination_reference_and_native_visitor(self):
        self.opener.cookies.update({"odoo_utm_campaign": "old-cookie-campaign"})
        result = self._post()
        self.assertEqual(result.status_code, 202, result.text)
        target = urlsplit(result.json()["url"])
        self.assertEqual(target.netloc, "wa.me")
        self.assertEqual(target.path, "/5519999999999")
        handoff = self._clicks()
        self.assertEqual(len(handoff), 1)
        self.assertRegex(handoff.reference, r"^CP-[A-Z0-9]{12}$")
        self.assertEqual(result.json()["reference"], handoff.reference)
        self.assertEqual(parse_qs(target.query)["text"], [
            self.action.whatsapp_message + "\n\nReferência: " + handoff.reference,
        ])
        self.assertEqual(handoff.account_id, self.account)
        self.assertTrue(handoff.visitor_id)
        self.assertRegex(handoff.session_key, r"^[a-f0-9]{64}$")
        self.assertEqual(handoff.acquisition_json["gclid"], "synthetic-click")
        self.assertNotIn("utm_campaign", handoff.acquisition_json)
        self.assertEqual(handoff.page_url, self.origin + self.page_path)
        self.assertNotIn("session", result.text)
        self.assertIn("no-store", result.headers["Cache-Control"])
        self.assertFalse(self.env["marketing.web.ingress.event"].search([
            ("endpoint_id", "=", self.endpoint.id),
        ]), "Native handoff must not restart legacy acquisition ingestion")

    def test_http_retry_and_rapid_second_click_keep_one_reference(self):
        first = self._post()
        self.assertEqual(first.status_code, 202, first.text)
        replay = self._post()
        self.assertEqual(replay.json(), first.json())
        self.event_id = str(uuid.uuid4())
        repeated = self._post()
        self.assertEqual(repeated.status_code, 202, repeated.text)
        self.assertEqual(repeated.json(), first.json())
        self.assertEqual(len(self._clicks()), 1)

    def test_retry_after_account_change_cannot_reuse_old_account_reference(self):
        first = self._post()
        self.assertEqual(first.status_code, 202, first.text)
        other_account = self.env["contact.center.account"].create({
            "name": "Other synthetic WhatsApp destination",
            "company_id": self.website.company_id.id,
            "platform": "whatsapp",
            "own_external_identity": "5519999999999@s.whatsapp.net",
        })
        self.action.handoff_account_id = other_account
        retry = self._post()
        self.assertEqual(retry.status_code, 400, retry.text)
        clicks = self._clicks()
        self.assertEqual(len(clicks), 1)
        self.assertEqual(clicks.account_id, self.account)

    def test_spoofed_identity_destination_origin_and_page_are_rejected(self):
        for extra in ({"visitor_id": 1}, {"session_key": "a" * 64}, {"url": "https://evil.invalid"},
                      {"text": "Browser-supplied text must never enter the capture service"}):
            result = self._post(payload={
                "action_ref": self.action.public_ref, "event_id": self.event_id, **extra,
            })
            self.assertEqual(result.status_code, 400, result.text)
        for headers, status in (
            ({"Origin": "https://evil.invalid"}, 404),
            ({"Purpose": "prefetch"}, 404),
            ({"X-Forwarded-Proto": "http"}, 404),
            ({"Referer": self.origin + "/different-page"}, 400),
            ({"Referer": "https://evil.invalid" + self.page_path}, 400),
        ):
            self.assertEqual(self._post(headers=headers).status_code, status)
        self.assertFalse(self._clicks())

    def test_current_policy_and_destination_drift_stop_capture(self):
        self.endpoint.capture_enabled = False
        self.assertEqual(self._post().status_code, 400)
        self.endpoint.capture_enabled = True
        self.action.handoff_enabled = False
        self.assertEqual(self._post().status_code, 400)
        self.action.handoff_enabled = True
        self.account.own_external_identity = "5519888888888@s.whatsapp.net"
        self.assertEqual(self._post().status_code, 400)
        self.assertFalse(self._clicks())

    def test_individual_consent_policy_without_grant_and_legacy_are_denied(self):
        self.binding.capture_mode = "legacy"
        self.assertEqual(self._post().status_code, 400)
        self.binding.capture_mode = "native"
        self.endpoint.write({
            "website_tracking_policy": "individual_consent",
            "privacy_legal_basis_code": "consent",
        })
        self.assertEqual(self._post().status_code, 400)
        self.assertFalse(self._clicks())

    def test_other_website_action_cannot_be_used_from_current_website(self):
        other_site = self.env["website"].create({
            "name": "Other synthetic website", "domain": "https://other-handoff.invalid",
            "company_id": self.website.company_id.id,
        })
        other_binding = self.env["marketing.website.ingress.binding"].create({
            "website_id": other_site.id, "endpoint_id": self.endpoint.id, "capture_mode": "native",
        })
        other_action = self.env["marketing.website.action"].create({
            "name": "Other website WhatsApp", "binding_id": other_binding.id,
            "kind": "whatsapp_handoff", "route_ref": "other.whatsapp.native.http",
            "source_path": self.page_path, "whatsapp_destination": "5519999999999",
            "handoff_enabled": True, "handoff_account_id": self.account.id,
        })
        result = self._post(payload={"action_ref": other_action.public_ref, "event_id": self.event_id})
        self.assertEqual(result.status_code, 400, result.text)
        self.assertFalse(self._clicks())

    def test_individual_cookie_grant_is_verified_and_revocation_stops_new_clicks(self):
        self.website.cookies_bar = True
        self.endpoint.write({
            "website_tracking_policy": "individual_consent", "privacy_legal_basis_code": "consent",
        })
        consent = self.env["marketing.website.consent"]
        decision = consent._decide(self.binding, True)
        cookie = decision._cookie()
        self.opener.cookies.update({
            "mc_website_consent": cookie, "website_cookies_bar": '{"required":true,"optional":true}',
        })
        first = self._post()
        self.assertEqual(first.status_code, 202, first.text)
        consent._decide(self.binding, False, previous_cookie=cookie)
        self.event_id = str(uuid.uuid4())
        self.assertEqual(self._post().status_code, 400)
        self.assertEqual(len(self._clicks()), 1)

    def test_application_failure_rolls_back_click_and_returns_fallback_signal(self):
        capture_type = type(self.env["marketing.website.whatsapp.handoff"])
        original = capture_type._record_click
        def create_then_fail(model, *args, **kwargs):
            original(model, *args, **kwargs)
            raise RuntimeError("Synthetic failure after write")
        with patch.object(capture_type, "_record_click", new=create_then_fail):
            result = self._post()
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json(), {"accepted": False})
        self.assertFalse(self._clicks())

    def test_rate_limit_returns_retry_hint_and_no_click(self):
        admission = type(self.env["marketing.web.ingress.admission"])
        with patch.object(admission, "_admit", return_value=False):
            response = self._post()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "60")
        self.assertFalse(self._clicks())

    def test_snapshot_uses_only_preceding_same_site_tracks_and_keeps_click_tuple(self):
        visitor = self.env["website.visitor"].create({"access_token": uuid.uuid4().hex})
        now = fields.Datetime.now()
        track_model = self.env["website.track"]
        original = track_model.create({
            "visitor_id": visitor.id, "url": self.origin + "/?gclid=first&gad_campaignid=123",
            "visit_datetime": now - datetime.timedelta(minutes=2),
        })
        track_model.create({
            "visitor_id": visitor.id, "url": self.origin + self.page_path,
            "visit_datetime": now - datetime.timedelta(minutes=1),
        })
        track_model.create({
            "visitor_id": visitor.id, "url": "https://other.invalid/?gclid=wrong-site",
            "visit_datetime": now - datetime.timedelta(seconds=30),
        })
        track_model.create({
            "visitor_id": visitor.id, "url": self.origin + "/?gclid=later-tab",
            "visit_datetime": now - datetime.timedelta(seconds=20),
        })
        snapshot = self.env["marketing.website.whatsapp.capture"]._snapshot(
            self.action, self.origin, self.origin + self.page_path, visitor=visitor,
            now=now, cookies={"odoo_utm_campaign": "old-cookie"},
        )
        self.assertEqual(snapshot["acquisition"]["gclid"], "first")
        self.assertNotIn("utm_campaign", snapshot["acquisition"])
        self.assertEqual(snapshot["track"], original)

    def test_configuration_rejects_wrong_account_and_prefix(self):
        self.assertEqual(account_whatsapp_digits(self.account), "5519999999999")
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.action.handoff_reference_prefix = "bad prefix"
        self.account.own_external_identity = "opaque@lid"
        self.assertEqual(account_whatsapp_digits(self.account), "")
        with self.assertRaises(ValidationError):
            self.action._check_handoff_configuration()

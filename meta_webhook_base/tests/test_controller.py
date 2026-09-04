import hashlib
import hmac
import json
import os
import urllib.parse

from odoo.tests.common import HttpCase


class TestMetaWebhookController(HttpCase):
    APP_SECRET_REF = "ODOO_META_WEBHOOK_HTTP_APP_SECRET"
    VERIFY_TOKEN_REF = "ODOO_META_WEBHOOK_HTTP_VERIFY_TOKEN"
    APP_SECRET = "synthetic-http-app-secret-12345"
    VERIFY_TOKEN = "synthetic-http-verify-token-12345"
    PAGE_ID = "100000000000301"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._previous = {
            cls.APP_SECRET_REF: os.environ.get(cls.APP_SECRET_REF),
            cls.VERIFY_TOKEN_REF: os.environ.get(cls.VERIFY_TOKEN_REF),
        }
        os.environ[cls.APP_SECRET_REF] = cls.APP_SECRET
        os.environ[cls.VERIFY_TOKEN_REF] = cls.VERIFY_TOKEN
        cls.app = cls.env["meta.api.app"].create(
            {
                "name": "Webhook HTTP App",
                "external_app_id": "100000000000201",
                "credential_backend": "environment",
                "app_secret_ref": cls.APP_SECRET_REF,
            }
        )
        cls.endpoint = cls.env["meta.webhook.endpoint"].create(
            {
                "name": "Webhook HTTP endpoint",
                "app_id": cls.app.id,
                "credential_backend": "environment",
                "verify_token_ref": cls.VERIFY_TOKEN_REF,
            }
        )
        cls.path = "/meta/webhook/%s" % cls.endpoint.routing_key

    @classmethod
    def tearDownClass(cls):
        for key, previous in cls._previous.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        super().tearDownClass()

    def _body(self):
        return json.dumps(
            {
                "object": "page",
                "entry": [
                    {
                        "id": self.PAGE_ID,
                        "changes": [
                            {
                                "field": "leadgen",
                                "value": {
                                    "leadgen_id": "200000000000301",
                                    "page_id": self.PAGE_ID,
                                    "form_id": "300000000000301",
                                    "created_time": 1_800_000_000,
                                    "email": "must-not-persist@example.invalid",
                                },
                            }
                        ],
                    }
                ],
            },
            separators=(",", ":"),
        ).encode()

    def _signature(self, body):
        return (
            "sha256=%s"
            % hmac.new(self.APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
        )

    def test_get_challenge_uses_external_verify_token(self):
        query = urllib.parse.urlencode(
            {
                "hub.mode": "subscribe",
                "hub.verify_token": self.VERIFY_TOKEN,
                "hub.challenge": "bounded-challenge",
            }
        )
        response = self.url_open("%s?%s" % (self.path, query))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "bounded-challenge")

    def test_get_challenge_rejects_unicode_verify_token_with_403(self):
        query = urllib.parse.urlencode(
            {
                "hub.mode": "subscribe",
                "hub.verify_token": "%s-é-🔐" % self.VERIFY_TOKEN,
                "hub.challenge": "must-not-be-returned",
            }
        )
        response = self.url_open("%s?%s" % (self.path, query))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.text, "Forbidden")

    def test_signed_exact_body_is_deduplicated_and_sanitized(self):
        body = self._body()
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": self._signature(body),
        }
        first = self.opener.post(
            self.base_url() + self.path, data=body, headers=headers
        )
        second = self.opener.post(
            self.base_url() + self.path, data=body, headers=headers
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertFalse(first.json()["duplicate"])
        self.assertTrue(second.json()["duplicate"])
        self.env.invalidate_all()
        deliveries = (
            self.env["meta.webhook.delivery"]
            .sudo()
            .search([("endpoint_id", "=", self.endpoint.id)])
        )
        self.assertEqual(len(deliveries), 1)
        self.assertNotIn(
            "must-not-persist",
            json.dumps(deliveries.sanitized_envelope_json),
        )
        self.assertEqual(deliveries.item_ids.kind, "leadgen")

    def test_signed_post_does_not_depend_on_challenge_verify_token(self):
        body = self._body()
        previous = os.environ.pop(self.VERIFY_TOKEN_REF)
        try:
            response = self.opener.post(
                self.base_url() + self.path,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": self._signature(body),
                },
            )
        finally:
            os.environ[self.VERIFY_TOKEN_REF] = previous

        self.assertEqual(response.status_code, 200, response.text)

    def test_invalid_signature_is_rejected_without_evidence(self):
        body = self._body()
        response = self.opener.post(
            self.base_url() + self.path,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=" + "0" * 64,
            },
        )
        self.assertEqual(response.status_code, 401)
        self.env.invalidate_all()
        self.assertFalse(
            self.env["meta.webhook.delivery"]
            .sudo()
            .search_count([("endpoint_id", "=", self.endpoint.id)])
        )

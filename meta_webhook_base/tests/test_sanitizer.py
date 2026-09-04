import json

from odoo.tests.common import BaseCase

from ..services.sanitizer import (
    MAX_CHANGES_PER_ENTRY,
    MAX_MESSAGING_PER_ENTRY,
    MAX_WEBHOOK_ITEMS,
    MetaWebhookSanitizationError,
    canonical_digest,
    sanitized_webhook,
    validate_sanitized_payload,
)


class TestMetaWebhookSanitizer(BaseCase):
    PAGE_ID = "100000000000101"

    def _leadgen(self):
        return {
            "object": "page",
            "ignored": "never-persist",
            "entry": [
                {
                    "id": self.PAGE_ID,
                    "time": 1_800_000_000,
                    "changes": [
                        {
                            "field": "leadgen",
                            "extra": "discard",
                            "value": {
                                "leadgen_id": "200000000000001",
                                "page_id": self.PAGE_ID,
                                "form_id": "300000000000001",
                                "created_time": 1_800_000_000,
                                "ad_id": "400000000000001",
                                "email": "discard@example.invalid",
                            },
                        }
                    ],
                }
            ],
        }

    def test_leadgen_allowlist_discards_unknown_and_pii(self):
        result = sanitized_webhook(self._leadgen())
        serialized = json.dumps(result.envelope, sort_keys=True)
        self.assertNotIn("never-persist", serialized)
        self.assertNotIn("discard@example.invalid", serialized)
        self.assertEqual(result.items[0]["kind"], "leadgen")

    def test_invalid_leadgen_is_quarantined_without_raw_value(self):
        value = self._leadgen()
        value["entry"][0]["changes"][0]["value"]["page_id"] = "999"
        result = sanitized_webhook(value)
        self.assertEqual(result.items[0]["kind"], "unknown")
        self.assertNotIn("leadgen_id", json.dumps(result.envelope))

    def test_non_finite_provider_timestamps_are_rejected_safely(self):
        for field, value in (
            ("time", float("inf")),
            ("created_time", float("-inf")),
        ):
            payload = self._leadgen()
            target = payload["entry"][0]
            if field == "created_time":
                target = target["changes"][0]["value"]
            target[field] = value
            with self.subTest(field=field), self.assertRaises(
                MetaWebhookSanitizationError
            ):
                sanitized_webhook(payload)

    def test_unsupported_change_discards_provider_value(self):
        value = self._leadgen()
        value["entry"][0]["changes"][0] = {
            "field": "feed",
            "value": {"access_token": "must-not-survive"},
        }
        result = sanitized_webhook(value)
        self.assertNotIn("must-not-survive", json.dumps(result.envelope))

    def test_messaging_content_is_not_in_shared_envelope(self):
        value = {
            "object": "page",
            "entry": [
                {
                    "id": self.PAGE_ID,
                    "messaging": [{"message": {"text": "private body"}}],
                }
            ],
        }
        result = sanitized_webhook(value)
        self.assertNotIn("private body", json.dumps(result.envelope))
        self.assertEqual(result.expected_messaging_keys, ("entry:0:messaging:0",))

    def test_bounds_entries_and_changes(self):
        with self.assertRaises(MetaWebhookSanitizationError):
            sanitized_webhook({"object": "page", "entry": "not-an-array"})
        value = self._leadgen()
        value["entry"][0]["changes"] = [{}] * (MAX_CHANGES_PER_ENTRY + 1)
        with self.assertRaises(MetaWebhookSanitizationError):
            sanitized_webhook(value)

        entries = [
            {
                "id": str(100000000000000 + index),
                "messaging": [{}] * MAX_MESSAGING_PER_ENTRY,
            }
            for index in range(MAX_WEBHOOK_ITEMS // MAX_MESSAGING_PER_ENTRY + 1)
        ]
        with self.assertRaisesRegex(MetaWebhookSanitizationError, "item count"):
            sanitized_webhook({"object": "page", "entry": entries})

    def test_consumer_payload_rejects_secret_and_url_keys(self):
        for payload in (
            {"access_token": "secret"},
            {"media": {"signed_url": "https://example.invalid"}},
            {"page_access_token_value": "secret"},
            {"request": {"authorization_header": "secret"}},
            {"password_hint": "secret"},
            {"session_cookie_value": "secret"},
            {"metric": float("inf")},
            {"metric": float("-inf")},
            {"metric": float("nan")},
        ):
            with self.subTest(payload=payload), self.assertRaises(
                MetaWebhookSanitizationError
            ):
                validate_sanitized_payload(payload)

    def test_consumer_payload_rejects_invalid_unicode_and_control_characters(self):
        for payload in (
            {"unsafe\nkey": "value"},
            {"unsafe\x7fkey": "value"},
            {"unsafe\ud800key": "value"},
            {"safe": "unsafe\ud800value"},
        ):
            with self.subTest(payload=repr(payload)), self.assertRaises(
                MetaWebhookSanitizationError
            ):
                validate_sanitized_payload(payload)

    def test_canonical_digest_is_order_stable(self):
        self.assertEqual(
            canonical_digest({"b": 2, "a": 1}), canonical_digest({"a": 1, "b": 2})
        )

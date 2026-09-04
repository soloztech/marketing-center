import hashlib
import hmac

from odoo.tests.common import SavepointCase

from ..services.signature import (
    MAX_WEBHOOK_BODY_BYTES,
    META_GRAPH_BASELINE_VERSION,
    normalized_header,
    validate_graph_version,
    verify_signature,
)


class TestMetaSignature(SavepointCase):
    SECRET = "synthetic-meta-app-secret-for-tests"

    @classmethod
    def _signature(cls, body):
        digest = hmac.new(cls.SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return "sha256=%s" % digest

    def test_signature_uses_exact_bytes_and_case_insensitive_header_name(self):
        body = b'{"entry":[],"object":"page"}'
        signature = self._signature(body)

        self.assertTrue(
            verify_signature(
                self.SECRET,
                {"x-HuB-sIgNaTuRe-256": signature},
                body,
            )
        )
        self.assertFalse(
            verify_signature(
                self.SECRET,
                {"X-Hub-Signature-256": signature},
                body + b" ",
            )
        )

    def test_signature_rejects_missing_malformed_or_wrong_credentials(self):
        body = b"{}"
        signature = self._signature(body)
        invalid_values = (
            ("", {"X-Hub-Signature-256": signature}, body),
            (None, {"X-Hub-Signature-256": signature}, body),
            (self.SECRET, {}, body),
            (self.SECRET, {"X-Hub-Signature-256": "sha1=invalid"}, body),
            (
                self.SECRET,
                {"X-Hub-Signature-256": signature.removeprefix("sha256=")},
                body,
            ),
            (self.SECRET, {"X-Hub-Signature-256": "sha256=xyz"}, body),
            (self.SECRET, {"X-Hub-Signature-256": signature}, "{}"),
            ("different-secret", {"X-Hub-Signature-256": signature}, body),
            ("invalid\ud800secret", {"X-Hub-Signature-256": signature}, body),
        )

        for secret, headers, candidate_body in invalid_values:
            with self.subTest(secret=bool(secret), body_type=type(candidate_body)):
                self.assertFalse(verify_signature(secret, headers, candidate_body))

    def test_signature_accepts_bytearray_without_changing_evidence(self):
        raw = b'{"object":"instagram","entry":[]}'
        body = bytearray(raw)

        self.assertTrue(
            verify_signature(
                self.SECRET,
                {
                    "X-Hub-Signature-256": "sha256=%s"
                    % self._signature(raw).removeprefix("sha256=").upper()
                },
                body,
            )
        )
        self.assertEqual(bytes(body), raw)

    def test_header_lookup_is_bounded_and_does_not_mutate_input(self):
        headers = {
            "Content-Type": " application/json ",
            "X-Hub-Signature-256": " signature ",
        }
        original = dict(headers)

        self.assertEqual(normalized_header(headers, "x-hub-signature-256"), "signature")
        self.assertEqual(normalized_header(headers, "missing"), "")
        self.assertEqual(headers, original)

    def test_graph_version_contract_and_limits_are_explicit(self):
        self.assertEqual(META_GRAPH_BASELINE_VERSION, "v26.0")
        self.assertEqual(MAX_WEBHOOK_BODY_BYTES, 2 * 1024 * 1024)
        for valid in ("v1.0", "v26.0", "v100.0"):
            with self.subTest(value=valid):
                self.assertTrue(validate_graph_version(valid))
        for invalid in (
            "26.0",
            "v0.0",
            "v26",
            "v26.1",
            "v100.12",
            "latest",
            "v26.0/path",
            None,
        ):
            with self.subTest(value=invalid):
                self.assertFalse(validate_graph_version(invalid))

    def test_signature_enforces_the_shared_webhook_body_limit(self):
        body = b"x" * MAX_WEBHOOK_BODY_BYTES
        signature = self._signature(body)

        self.assertTrue(
            verify_signature(
                self.SECRET,
                {"X-Hub-Signature-256": signature},
                body,
            )
        )
        self.assertFalse(
            verify_signature(
                self.SECRET,
                {"X-Hub-Signature-256": signature},
                body + b"x",
            )
        )

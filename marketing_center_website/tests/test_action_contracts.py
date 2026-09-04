import datetime
import json

from werkzeug.wrappers import Response

from odoo.tests.common import SavepointCase

from ..controllers.website_action import _append_form_receipt, _response_text
from ..services.contracts import (
    WebsiteActionContractError,
    parse_form_exchange,
    parse_whatsapp_claim,
    safe_relative_path,
)


class TestMarketingWebsiteActionContracts(SavepointCase):
    def test_receipt_is_exposed_only_on_native_form_success(self):
        receipt = "1788264000.1788264120.%s" % ("a" * 64)
        success = _append_form_receipt('{"id":42}', receipt)
        self.assertEqual(
            success,
            '{"id":42,"marketing_center_receipt":"%s"}' % receipt,
        )
        native_error = '{"error_fields":{"email":"invalid"}}'
        self.assertEqual(_append_form_receipt(native_error, receipt), native_error)

    def test_receipt_preserves_native_http_response(self):
        receipt = "1788264000.1788264120.%s" % ("b" * 64)
        response = Response('{"id":42}', content_type="application/json")

        result = _append_form_receipt(response, receipt)

        self.assertIs(result, response)
        self.assertEqual(_response_text(result), result.get_data(as_text=True))
        self.assertEqual(
            json.loads(result.get_data(as_text=True)),
            {"id": 42, "marketing_center_receipt": receipt},
        )

    def test_receipt_leaves_native_http_error_unchanged(self):
        receipt = "1788264000.1788264120.%s" % ("c" * 64)
        native_error = '{"error_fields":{"email":"invalid"}}'
        response = Response(native_error, content_type="application/json")

        result = _append_form_receipt(response, receipt)

        self.assertIs(result, response)
        self.assertEqual(result.get_data(as_text=True), native_error)

    def test_action_envelopes_accept_only_opaque_exact_fields(self):
        form = parse_form_exchange(
            {
                "action_ref": "11111111-1111-4111-8111-111111111111",
                "event_id": "22222222-2222-4222-8222-222222222222",
                "session_ref": "33333333-3333-4333-8333-333333333333",
                "receipt": "%s.%s.%s"
                % (
                    int(datetime.datetime(2026, 9, 1, 12, 0).timestamp()),
                    int(datetime.datetime(2026, 9, 1, 12, 2).timestamp()),
                    "a" * 64,
                ),
            }
        )
        self.assertEqual(form["action_ref"], "11111111-1111-4111-8111-111111111111")
        claim = parse_whatsapp_claim(
            {
                "action_ref": form["action_ref"],
                "event_id": form["event_id"],
                "session_ref": form["session_ref"],
            }
        )
        self.assertEqual(claim["session_ref"], form["session_ref"])

        for forbidden in ("email", "phone", "name", "form_values", "next"):
            payload = {
                "action_ref": form["action_ref"],
                "event_id": form["event_id"],
                "session_ref": form["session_ref"],
                forbidden: "private",
            }
            with self.subTest(forbidden=forbidden), self.assertRaises(
                WebsiteActionContractError
            ):
                parse_whatsapp_claim(payload)

    def test_configured_paths_are_relative_queryless_and_same_origin(self):
        self.assertEqual(safe_relative_path("/contactus", "path"), "/contactus")
        for value in (
            "https://evil.example/",
            "//evil.example/",
            "/contactus?email=private",
            "/contactus#private",
            "/line\nbreak",
            "/%2f%2fevil.example",
            "/%5cevil.example",
            "/%0d%0aheader",
            "contactus",
        ):
            with self.subTest(value=value), self.assertRaises(
                WebsiteActionContractError
            ):
                safe_relative_path(value, "path")

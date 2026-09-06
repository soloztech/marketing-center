import hashlib
import hmac
import json
import math
import pickle
from unittest import mock

import requests

from odoo.tests.common import SavepointCase

from ..services.credentials import MetaRuntimeApp
from ..services.errors import (
    MetaApiError,
    MetaApiPausedError,
    MetaApiRateLimitError,
    MetaApiTransientError,
    MetaApiUncertainError,
)
from ..services.graph import (
    graph_app_access_token,
    graph_appsecret_proof,
    graph_debug_token,
    graph_request,
)

REQUEST_PATCH = "odoo.addons.meta_api_base.services.graph.requests.request"


class FakeGraphApp:
    def __init__(
        self,
        *,
        external_app_id="100000000000001",
        graph_version="v26.0",
        app_secret="synthetic-meta-app-secret-for-tests",
        active=True,
    ):
        self.external_app_id = external_app_id
        self.graph_version = graph_version
        self.app_secret = app_secret
        self.active = active

    def ensure_one(self):
        return self


class FakeGraphResponse:
    def __init__(
        self,
        status_code=200,
        *,
        payload=None,
        content=None,
        headers=None,
        stream_error=None,
    ):
        self.status_code = status_code
        self.headers = dict(headers or {})
        if content is None:
            content = json.dumps(payload if payload is not None else {}).encode("utf-8")
        self.content = content
        self.stream_error = stream_error
        self.closed = False
        self.iterated = False

    def iter_content(self, chunk_size=16 * 1024):
        self.iterated = True
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]
        if self.stream_error:
            raise self.stream_error

    def close(self):
        self.closed = True


class TestMetaGraphClient(SavepointCase):
    APP_SECRET = "synthetic-meta-app-secret-for-tests"
    PAGE_TOKEN = "synthetic-page-token-that-must-stay-private"
    PAGE_ID = "100000000000001"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = FakeGraphApp()

    @mock.patch(REQUEST_PATCH)
    def test_private_multipart_upload_is_bounded_and_uses_bearer_and_form_proof(
        self, request
    ):
        response = FakeGraphResponse(payload={"attachment_id": "123456789"})
        request.return_value = response
        result = graph_request(
            self.app,
            self.PAGE_TOKEN,
            "POST",
            "%s/message_attachments" % self.PAGE_ID,
            data={"message": '{"attachment":{"type":"image"}}'},
            files={"filedata": ("image.png", b"synthetic-image", "image/png")},
        )
        self.assertEqual(result, {"attachment_id": "123456789"})
        self.assertEqual(
            request.call_args.kwargs["files"],
            {"filedata": ("image.png", b"synthetic-image", "image/png")},
        )
        self.assertIn("appsecret_proof", request.call_args.kwargs["data"])
        self.assertNotIn("Content-Type", request.call_args.kwargs["headers"])
        self.assertTrue(response.closed)

    @mock.patch(REQUEST_PATCH)
    def test_multipart_rejects_nonlocal_unbounded_or_conflicting_uploads_before_network(
        self, request
    ):
        invalid = (
            {"method": "GET", "files": {"filedata": ("safe.png", b"x", "image/png")}},
            {"json_data": {}, "files": {"filedata": ("safe.png", b"x", "image/png")}},
            {"files": {"filedata": ("../unsafe.png", b"x", "image/png")}},
            {
                "files": {
                    "filedata": (
                        "safe.png",
                        "https://example.invalid/image",
                        "image/png",
                    )
                }
            },
            {"files": {"filedata": ("safe.png", b"", "image/png")}},
            {
                "files": {
                    "filedata": ("safe.png", b"x" * (25 * 1024 * 1024 + 1), "image/png")
                }
            },
            {"files": {"filedata": ("safe.png", b"x", "image/png\r\nX: bad")}},
            {
                "files": {
                    "filedata": ("safe.png", b"x", "image/png"),
                    "second": ("safe.png", b"x", "image/png"),
                }
            },
        )
        for values in invalid:
            with self.subTest(case=list(values)):
                method = values.get("method", "POST")
                with self.assertRaises(MetaApiError):
                    graph_request(
                        self.app,
                        self.PAGE_TOKEN,
                        method,
                        "%s/message_attachments" % self.PAGE_ID,
                        files=values["files"],
                        json_data=values.get("json_data"),
                    )
        request.assert_not_called()

    def test_proof_and_app_token_are_built_only_in_memory(self):
        expected = hmac.new(
            self.APP_SECRET.encode("utf-8"),
            self.PAGE_TOKEN.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        self.assertEqual(
            graph_appsecret_proof(self.APP_SECRET, self.PAGE_TOKEN), expected
        )
        self.assertEqual(len(expected), 64)
        self.assertEqual(
            graph_app_access_token(self.app),
            "%s|%s" % (self.PAGE_ID, self.APP_SECRET),
        )
        for app_secret, token in (("", self.PAGE_TOKEN), (self.APP_SECRET, "")):
            with self.subTest(app_secret=bool(app_secret), token=bool(token)):
                with self.assertRaises(MetaApiPausedError):
                    graph_appsecret_proof(app_secret, token)
        with self.assertRaises(MetaApiPausedError):
            graph_app_access_token(FakeGraphApp(app_secret=""))

    def test_graph_helpers_accept_the_shared_runtime_app_contract(self):
        runtime = MetaRuntimeApp(
            active=True,
            external_app_id=self.PAGE_ID,
            graph_version="v26.0",
            app_secret=self.APP_SECRET,
            public_ref="00000000-0000-4000-8000-000000000001",
            revision=3,
            company_id=1,
        )

        self.assertIs(runtime.ensure_one(), runtime)
        self.assertEqual(
            graph_app_access_token(runtime),
            "%s|%s" % (self.PAGE_ID, self.APP_SECRET),
        )
        self.assertNotIn(self.APP_SECRET, repr(runtime))
        with self.assertRaises(TypeError):
            pickle.dumps(runtime)

    def test_graph_helpers_reject_objects_without_a_single_app_contract(self):
        incomplete = mock.Mock(spec=["ensure_one"])
        incomplete.ensure_one.return_value = incomplete
        unicode_digits = FakeGraphApp(external_app_id="１０００００００００００００１")
        for candidate in (object(), incomplete, unicode_digits):
            with self.subTest(candidate=candidate), self.assertRaisesRegex(
                MetaApiError, "runtime is invalid"
            ):
                graph_app_access_token(candidate)

    def test_get_uses_fixed_url_bearer_proof_and_copies_caller_params(self):
        response = FakeGraphResponse(payload={"id": self.PAGE_ID})
        params = {"fields": "id,name"}

        with mock.patch(REQUEST_PATCH, return_value=response) as request_mock:
            result = graph_request(
                self.app,
                self.PAGE_TOKEN,
                "GET",
                "%s/subscribed_apps" % self.PAGE_ID,
                params=params,
            )

        self.assertEqual(result, {"id": self.PAGE_ID})
        args = request_mock.call_args.args
        kwargs = request_mock.call_args.kwargs
        self.assertEqual(args[0], "GET")
        self.assertEqual(
            args[1],
            "https://graph.facebook.com/v26.0/%s/subscribed_apps" % self.PAGE_ID,
        )
        self.assertNotIn(self.PAGE_TOKEN, args[1])
        self.assertNotIn(self.APP_SECRET, args[1])
        self.assertEqual(
            kwargs["headers"]["Authorization"], "Bearer %s" % self.PAGE_TOKEN
        )
        self.assertEqual(kwargs["headers"]["Accept"], "application/json")
        self.assertEqual(kwargs["params"]["fields"], "id,name")
        self.assertEqual(
            kwargs["params"]["appsecret_proof"],
            graph_appsecret_proof(self.APP_SECRET, self.PAGE_TOKEN),
        )
        self.assertEqual(params, {"fields": "id,name"})
        self.assertIsNone(kwargs["data"])
        self.assertIsNone(kwargs["json"])
        self.assertEqual(kwargs["timeout"], (5, 20))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        self.assertTrue(response.closed)

    def test_post_supports_form_or_json_and_puts_proof_in_the_body(self):
        cases = (
            (
                {"data": {"recipient": "recipient", "message": "hello"}},
                "data",
            ),
            (
                {
                    "json_data": {
                        "recipient": {"id": "recipient"},
                        "message": {"text": "hello"},
                    }
                },
                "json",
            ),
        )

        for values, protected_channel in cases:
            response = FakeGraphResponse(payload={"message_id": "mid.synthetic"})
            original = json.loads(json.dumps(next(iter(values.values()))))
            with self.subTest(channel=protected_channel), mock.patch(
                REQUEST_PATCH, return_value=response
            ) as request_mock:
                result = graph_request(
                    self.app,
                    self.PAGE_TOKEN,
                    "POST",
                    "%s/messages" % self.PAGE_ID,
                    mutating=True,
                    **values,
                )

            self.assertEqual(result, {"message_id": "mid.synthetic"})
            kwargs = request_mock.call_args.kwargs
            self.assertEqual(
                kwargs[protected_channel]["appsecret_proof"],
                graph_appsecret_proof(self.APP_SECRET, self.PAGE_TOKEN),
            )
            other_channel = "json" if protected_channel == "data" else "data"
            self.assertIsNone(kwargs[other_channel])
            self.assertEqual(next(iter(values.values())), original)
            self.assertTrue(response.closed)

    def test_explicit_empty_json_keeps_its_selected_body_channel(self):
        response = FakeGraphResponse(payload={"success": True})
        with mock.patch(REQUEST_PATCH, return_value=response) as request_mock:
            graph_request(
                self.app,
                self.PAGE_TOKEN,
                "POST",
                "%s/subscribed_apps" % self.PAGE_ID,
                json_data={},
                mutating=True,
            )

        request_values = request_mock.call_args.kwargs
        self.assertIsNone(request_values["data"])
        self.assertEqual(
            request_values["json"]["appsecret_proof"],
            graph_appsecret_proof(self.APP_SECRET, self.PAGE_TOKEN),
        )

    def test_get_rejects_explicit_body_even_when_empty(self):
        for values in ({"data": {}}, {"json_data": {}}):
            with self.subTest(values=values), mock.patch(
                REQUEST_PATCH
            ) as request_mock, self.assertRaisesRegex(
                MetaApiError, "GET requests cannot carry a body"
            ):
                graph_request(
                    self.app,
                    self.PAGE_TOKEN,
                    "GET",
                    self.PAGE_ID,
                    **values,
                )
            request_mock.assert_not_called()

    def test_debug_token_keeps_credentials_out_of_the_path_and_headers_are_private(
        self,
    ):
        response = FakeGraphResponse(payload={"data": {"is_valid": True}})

        with mock.patch(REQUEST_PATCH, return_value=response) as request_mock:
            result = graph_debug_token(self.app, self.PAGE_TOKEN)

        self.assertTrue(result["data"]["is_valid"])
        args = request_mock.call_args.args
        kwargs = request_mock.call_args.kwargs
        app_token = "%s|%s" % (self.PAGE_ID, self.APP_SECRET)
        self.assertEqual(args[1], "https://graph.facebook.com/v26.0/debug_token")
        self.assertNotIn(self.PAGE_TOKEN, args[1])
        self.assertNotIn(app_token, args[1])
        self.assertEqual(kwargs["params"]["input_token"], self.PAGE_TOKEN)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer %s" % app_token)

        prepared = requests.Request(
            args[0], args[1], params=kwargs["params"], headers=kwargs["headers"]
        ).prepare()
        self.assertIn(self.PAGE_TOKEN, prepared.url)
        self.assertNotIn(self.APP_SECRET, prepared.url)

    def test_protected_credential_channels_cannot_be_overridden(self):
        attempts = (
            {"params": {"access_token": self.PAGE_TOKEN}},
            {"params": {"appsecret_proof": "forged"}},
            {"data": {"APP_SECRET": self.APP_SECRET}},
            {"data": {"client_secret": self.APP_SECRET}},
            {"json_data": {"access_token": self.PAGE_TOKEN}},
            {"json_data": {"recipient": {"client_secret": self.APP_SECRET}}},
            {"data": {"field": "form"}, "json_data": {"field": "json"}},
        )

        for values in attempts:
            with self.subTest(values=values), mock.patch(
                REQUEST_PATCH
            ) as request_mock, self.assertRaises(MetaApiError):
                graph_request(
                    self.app,
                    self.PAGE_TOKEN,
                    "GET",
                    self.PAGE_ID,
                    **values,
                )
            request_mock.assert_not_called()

    def test_declared_and_streamed_oversize_responses_are_rejected(self):
        declared = FakeGraphResponse(
            payload={"ok": True}, headers={"Content-Length": "2048"}
        )
        streamed = FakeGraphResponse(content=b'"' + (b"x" * 2048) + b'"')

        for response in (declared, streamed):
            with self.subTest(response=response), mock.patch(
                REQUEST_PATCH, return_value=response
            ), self.assertRaisesRegex(MetaApiError, "response is too large"):
                graph_request(
                    self.app,
                    self.PAGE_TOKEN,
                    "GET",
                    self.PAGE_ID,
                    max_response_bytes=1024,
                )
            self.assertTrue(response.closed)
        self.assertFalse(declared.iterated)
        self.assertTrue(streamed.iterated)

    def test_empty_204_is_accepted_and_invalid_json_shapes_are_rejected(self):
        empty = FakeGraphResponse(status_code=204, content=b"")
        with mock.patch(REQUEST_PATCH, return_value=empty):
            self.assertEqual(
                graph_request(
                    self.app,
                    self.PAGE_TOKEN,
                    "DELETE",
                    self.PAGE_ID,
                    mutating=True,
                ),
                {},
            )
        self.assertTrue(empty.closed)

        invalid_responses = (
            FakeGraphResponse(content=b"not-json"),
            FakeGraphResponse(content=b"[]"),
            FakeGraphResponse(content=b'"scalar"'),
            FakeGraphResponse(content=b"\xff"),
        )
        for response in invalid_responses:
            with self.subTest(content=response.content), mock.patch(
                REQUEST_PATCH, return_value=response
            ), self.assertRaisesRegex(MetaApiError, "response is invalid"):
                graph_request(self.app, self.PAGE_TOKEN, "GET", self.PAGE_ID)
            self.assertTrue(response.closed)

    def test_redirect_and_generic_provider_rejection_are_permanent(self):
        responses = (
            FakeGraphResponse(status_code=302, payload={}),
            FakeGraphResponse(status_code=400, payload={"error": {"code": 100}}),
        )
        for response in responses:
            with self.subTest(status=response.status_code), mock.patch(
                REQUEST_PATCH, return_value=response
            ), self.assertRaises(MetaApiError) as raised:
                graph_request(self.app, self.PAGE_TOKEN, "GET", self.PAGE_ID)
            self.assertEqual(raised.exception.classification, "permanent")
            self.assertTrue(response.closed)

    def test_non_finite_provider_error_codes_are_normalized(self):
        response = FakeGraphResponse(
            status_code=400,
            payload={
                "error": {
                    "code": float("inf"),
                    "error_subcode": float("-inf"),
                }
            },
        )
        with mock.patch(REQUEST_PATCH, return_value=response), self.assertRaises(
            MetaApiError
        ) as raised:
            graph_request(self.app, self.PAGE_TOKEN, "GET", self.PAGE_ID)

        self.assertEqual(raised.exception.provider_code, 0)
        self.assertEqual(raised.exception.provider_subcode, 0)
        self.assertTrue(response.closed)

    def test_authentication_and_rate_limit_errors_have_stable_classes(self):
        authentication = (
            FakeGraphResponse(status_code=401, content=b"<html>proxy</html>"),
            FakeGraphResponse(status_code=403, content=b""),
            FakeGraphResponse(status_code=400, payload={"error": {"code": 190}}),
        )
        for response in authentication:
            with self.subTest(status=response.status_code), mock.patch(
                REQUEST_PATCH, return_value=response
            ), self.assertRaises(MetaApiPausedError):
                graph_request(self.app, self.PAGE_TOKEN, "GET", self.PAGE_ID)
            self.assertTrue(response.closed)

        rate_limits = (
            FakeGraphResponse(
                status_code=429, content=b"", headers={"Retry-After": "9"}
            ),
            FakeGraphResponse(
                status_code=403,
                payload={"error": {"code": 613}},
                headers={"Retry-After": "7200"},
            ),
            FakeGraphResponse(status_code=400, payload={"error": {"code": 4}}),
        )
        expected_retry = (9, 3600, 60)
        for response, retry_after in zip(rate_limits, expected_retry):
            with self.subTest(status=response.status_code), mock.patch(
                REQUEST_PATCH, return_value=response
            ), self.assertRaises(MetaApiRateLimitError) as raised:
                graph_request(self.app, self.PAGE_TOKEN, "GET", self.PAGE_ID)
            self.assertEqual(raised.exception.retry_after_seconds, retry_after)
            self.assertEqual(raised.exception.classification, "rate_limited")
            self.assertIsInstance(raised.exception, MetaApiTransientError)
            self.assertNotIsInstance(raised.exception, MetaApiPausedError)
            self.assertTrue(response.closed)

    def test_business_use_case_rate_limits_preserve_safe_metadata(self):
        provider_message = "provider detail containing a secret-like value"
        for code in range(80_000, 80_015):
            response = FakeGraphResponse(
                status_code=400,
                payload={
                    "error": {
                        "code": code,
                        "error_subcode": 2446079,
                        "message": provider_message,
                    }
                },
                headers={"Retry-After": "27"},
            )
            with self.subTest(code=code), mock.patch(
                REQUEST_PATCH, return_value=response
            ), self.assertRaises(MetaApiRateLimitError) as raised:
                graph_request(self.app, self.PAGE_TOKEN, "GET", self.PAGE_ID)
            error = raised.exception
            self.assertEqual(error.classification, "rate_limited")
            self.assertEqual(error.retry_after_seconds, 27)
            self.assertEqual(error.http_status, 400)
            self.assertEqual(error.provider_code, code)
            self.assertEqual(error.provider_subcode, 2446079)
            self.assertNotIn(provider_message, str(error))
            self.assertTrue(response.closed)

        for code in (79_999, 80_015):
            response = FakeGraphResponse(
                status_code=400,
                payload={"error": {"code": code, "error_subcode": 2446079}},
            )
            with self.subTest(outside_code=code), mock.patch(
                REQUEST_PATCH, return_value=response
            ), self.assertRaises(MetaApiError) as raised:
                graph_request(self.app, self.PAGE_TOKEN, "GET", self.PAGE_ID)
            self.assertNotIsInstance(raised.exception, MetaApiRateLimitError)
            self.assertEqual(raised.exception.provider_code, code)
            self.assertTrue(response.closed)

    def test_server_and_network_failure_distinguish_reads_from_mutations(self):
        for mutating, expected_error in (
            (False, MetaApiTransientError),
            (True, MetaApiUncertainError),
        ):
            server = FakeGraphResponse(
                status_code=503,
                content=b"<html>unavailable</html>",
                headers={"Retry-After": "17"},
            )
            with self.subTest(kind="server", mutating=mutating), mock.patch(
                REQUEST_PATCH, return_value=server
            ), self.assertRaises(expected_error) as raised:
                graph_request(
                    self.app,
                    self.PAGE_TOKEN,
                    "POST" if mutating else "GET",
                    self.PAGE_ID,
                    mutating=mutating,
                )
            self.assertEqual(raised.exception.retry_after_seconds, 17)
            self.assertTrue(server.closed)

            leaked = requests.Timeout(
                "https://graph.facebook.com/?access_token=%s" % self.PAGE_TOKEN
            )
            with self.subTest(kind="network", mutating=mutating), mock.patch(
                REQUEST_PATCH, side_effect=leaked
            ), self.assertRaises(expected_error) as raised:
                graph_request(
                    self.app,
                    self.PAGE_TOKEN,
                    "POST" if mutating else "GET",
                    self.PAGE_ID,
                    mutating=mutating,
                )
            self.assertNotIn(self.PAGE_TOKEN, str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)

    def test_post_and_delete_are_always_mutations_even_without_the_flag(self):
        for method in ("POST", "DELETE"):
            response = FakeGraphResponse(status_code=503, content=b"")
            with self.subTest(method=method), mock.patch(
                REQUEST_PATCH, return_value=response
            ), self.assertRaises(MetaApiUncertainError):
                graph_request(self.app, self.PAGE_TOKEN, method, self.PAGE_ID)

    def test_http_timeout_distinguishes_reads_from_mutations(self):
        for status in (408, 425):
            for method, expected_error in (
                ("GET", MetaApiTransientError),
                ("POST", MetaApiUncertainError),
            ):
                response = FakeGraphResponse(status_code=status, content=b"")
                with self.subTest(status=status, method=method), mock.patch(
                    REQUEST_PATCH, return_value=response
                ), self.assertRaises(expected_error) as raised:
                    graph_request(self.app, self.PAGE_TOKEN, method, self.PAGE_ID)
                self.assertEqual(raised.exception.http_status, status)

    def test_permission_errors_pause_with_safe_provider_metadata(self):
        response = FakeGraphResponse(
            status_code=400,
            payload={"error": {"code": 200, "error_subcode": 2018065}},
        )
        with mock.patch(REQUEST_PATCH, return_value=response), self.assertRaises(
            MetaApiPausedError
        ) as raised:
            graph_request(self.app, self.PAGE_TOKEN, "GET", self.PAGE_ID)
        self.assertEqual(raised.exception.http_status, 400)
        self.assertEqual(raised.exception.provider_code, 200)
        self.assertEqual(raised.exception.provider_subcode, 2018065)

    def test_transient_graph_and_stream_failures_are_sanitized(self):
        graph_transient = FakeGraphResponse(
            status_code=400,
            payload={"error": {"code": 2, "is_transient": True}},
            headers={"Retry-After": "13"},
        )
        with mock.patch(REQUEST_PATCH, return_value=graph_transient), self.assertRaises(
            MetaApiTransientError
        ) as raised:
            graph_request(self.app, self.PAGE_TOKEN, "GET", self.PAGE_ID)
        self.assertEqual(raised.exception.retry_after_seconds, 13)

        leaked = requests.ConnectionError("prepared-url=%s" % self.PAGE_TOKEN)
        stream_failure = FakeGraphResponse(content=b'{"ok":', stream_error=leaked)
        with mock.patch(REQUEST_PATCH, return_value=stream_failure), self.assertRaises(
            MetaApiTransientError
        ) as raised:
            graph_request(self.app, self.PAGE_TOKEN, "GET", self.PAGE_ID)
        self.assertNotIn(self.PAGE_TOKEN, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(stream_failure.closed)

    def test_unverifiable_mutation_response_is_uncertain(self):
        invalid = FakeGraphResponse(status_code=200, content=b"not-json")

        with mock.patch(REQUEST_PATCH, return_value=invalid), self.assertRaises(
            MetaApiUncertainError
        ) as raised:
            graph_request(
                self.app,
                self.PAGE_TOKEN,
                "POST",
                self.PAGE_ID,
                data={"message": "hello"},
                mutating=True,
            )

        self.assertEqual(raised.exception.classification, "uncertain")
        self.assertIsInstance(raised.exception.__cause__, MetaApiError)
        self.assertTrue(invalid.closed)

    def test_deep_json_is_sanitized_and_a_write_remains_uncertain(self):
        raw = (b"[" * 1100) + b"0" + (b"]" * 1100)
        for method, expected_error in (
            ("GET", MetaApiError),
            ("POST", MetaApiUncertainError),
        ):
            response = FakeGraphResponse(status_code=200, content=raw)
            with self.subTest(method=method), mock.patch(
                REQUEST_PATCH, return_value=response
            ), self.assertRaises(expected_error) as raised:
                graph_request(self.app, self.PAGE_TOKEN, method, self.PAGE_ID)
            self.assertNotIn("[[", str(raised.exception))
            self.assertTrue(response.closed)

    def test_invalid_inputs_fail_before_network(self):
        attempts = (
            {"method": "PATCH", "path": self.PAGE_ID},
            {"method": "GET", "path": "../secrets"},
            {"method": "GET", "path": "id?access_token=secret"},
            {"method": "GET", "path": ""},
            {"method": "GET", "path": self.PAGE_ID, "max_response_bytes": 100},
            {"method": "GET", "path": self.PAGE_ID, "params": "fields=id"},
            {"method": "GET", "path": "x" * 2049},
            {"method": "GET", "path": "invalid\ud800path"},
            {
                "method": "GET",
                "path": self.PAGE_ID,
                "params": {"value": math.nan},
            },
            {
                "method": "GET",
                "path": self.PAGE_ID,
                "params": {"invalid\ud800key": "value"},
            },
            {
                "method": "GET",
                "path": self.PAGE_ID,
                "params": {"invalid\x7fkey": "value"},
            },
            {
                "method": "GET",
                "path": self.PAGE_ID,
                "params": {"value": 1 << 4097},
            },
            {
                "method": "POST",
                "path": self.PAGE_ID,
                "json_data": {"value": "x" * (2 * 1024 * 1024)},
            },
            {
                "method": "POST",
                "path": self.PAGE_ID,
                "json_data": {"value": object()},
            },
            {
                "method": "GET",
                "path": self.PAGE_ID,
                "max_response_bytes": 1024 * 1024 + 1,
            },
        )
        invalid_apps = (
            FakeGraphApp(graph_version="latest"),
            FakeGraphApp(graph_version="v26.1"),
            FakeGraphApp(active=False),
        )

        for values in attempts:
            with self.subTest(values=values), mock.patch(
                REQUEST_PATCH
            ) as request_mock, self.assertRaises(MetaApiError):
                graph_request(self.app, self.PAGE_TOKEN, **values)
            request_mock.assert_not_called()
        for app in invalid_apps:
            with self.subTest(app=app.__dict__), mock.patch(
                REQUEST_PATCH
            ) as request_mock, self.assertRaises(MetaApiError):
                graph_request(app, self.PAGE_TOKEN, "GET", self.PAGE_ID)
            request_mock.assert_not_called()
        with mock.patch(REQUEST_PATCH) as request_mock, self.assertRaises(
            MetaApiPausedError
        ):
            graph_debug_token(self.app, "")
        request_mock.assert_not_called()

    def test_error_metadata_is_bounded_and_typed(self):
        error = MetaApiError(
            "safe",
            retry_after_seconds=10**9,
            http_status=-1,
            provider_code=True,
            provider_subcode=10**20,
        )

        self.assertEqual(error.retry_after_seconds, 86_400)
        self.assertEqual(error.http_status, 0)
        self.assertEqual(error.provider_code, 0)
        self.assertEqual(error.provider_subcode, 2_147_483_647)

        overflow = MetaApiError(
            "safe",
            retry_after_seconds=float("inf"),
            http_status=float("-inf"),
            provider_code=float("nan"),
        )
        self.assertEqual(overflow.retry_after_seconds, 0)
        self.assertEqual(overflow.http_status, 0)
        self.assertEqual(overflow.provider_code, 0)

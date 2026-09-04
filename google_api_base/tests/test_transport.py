from datetime import datetime, timezone
from unittest import mock

import requests

from odoo.tests.common import SavepointCase

from ..services.credentials import GoogleRuntimeIdentity
from ..services.errors import (
    GoogleApiError,
    GoogleApiLimitError,
    GoogleApiPermissionError,
    GoogleApiRateLimitError,
    GoogleApiTransientError,
)
from ..services.http import retry_after_seconds
from ..services.transport import (
    list_accessible_customers_request,
    normalize_customer_id,
    search_request,
)
from .common import FakeResponse

REQUEST_PATCH = "odoo.addons.google_api_base.services.transport.requests.request"


class TestGoogleAdsTransport(SavepointCase):
    ACCESS_TOKEN = "synthetic-short-lived-access-token"
    DEVELOPER_TOKEN = "synthetic-developer-token"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.identity = GoogleRuntimeIdentity(
            active=True,
            api_version="v25",
            oauth_client_id="synthetic-client-id.apps.googleusercontent.com",
            oauth_client_secret="synthetic-client-secret",
            refresh_token="synthetic-refresh-token",
            developer_token=cls.DEVELOPER_TOKEN,
            login_customer_id="1234567890",
            public_ref="00000000-0000-4000-8000-000000000001",
            revision=1,
            company_id=1,
        )

    def test_discovery_uses_fixed_v25_url_and_omits_irrelevant_login_header(self):
        response = FakeResponse(
            payload={"resourceNames": ["customers/2345678901"]},
            headers={"request-id": "request-list-1"},
        )
        with mock.patch(REQUEST_PATCH, return_value=response) as request_mock:
            payload, request_id = list_accessible_customers_request(
                self.identity, self.ACCESS_TOKEN
            )

        args = request_mock.call_args.args
        kwargs = request_mock.call_args.kwargs
        self.assertEqual(args[0], "GET")
        self.assertEqual(
            args[1],
            "https://googleads.googleapis.com/v25/customers:listAccessibleCustomers",
        )
        self.assertEqual(
            kwargs["headers"]["Authorization"], "Bearer %s" % self.ACCESS_TOKEN
        )
        self.assertEqual(kwargs["headers"]["developer-token"], self.DEVELOPER_TOKEN)
        self.assertNotIn("login-customer-id", kwargs["headers"])
        self.assertIsNone(kwargs["json"])
        self.assertEqual(kwargs["timeout"], (5, 30))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        self.assertEqual(payload["resourceNames"], ["customers/2345678901"])
        self.assertEqual(request_id, "request-list-1")
        self.assertTrue(response.closed)

    def test_search_uses_fixed_path_query_body_and_normalized_manager_header(self):
        response = FakeResponse(payload={"results": [], "fieldMask": "campaign.id"})
        with mock.patch(REQUEST_PATCH, return_value=response) as request_mock:
            search_request(
                self.identity,
                self.ACCESS_TOKEN,
                "234-567-8901",
                "SELECT campaign.id FROM campaign",
                page_token="opaque-next-token",
            )

        args = request_mock.call_args.args
        kwargs = request_mock.call_args.kwargs
        self.assertEqual(args[0], "POST")
        self.assertEqual(
            args[1],
            "https://googleads.googleapis.com/v25/customers/2345678901/googleAds:search",
        )
        self.assertEqual(kwargs["headers"]["login-customer-id"], "1234567890")
        self.assertEqual(
            kwargs["json"],
            {
                "query": "SELECT campaign.id FROM campaign",
                "pageToken": "opaque-next-token",
            },
        )

    def test_error_details_are_allow_listed_without_provider_messages(self):
        secret_message = "customer payload must never reach diagnostics"
        response = FakeResponse(
            status_code=403,
            payload={
                "error": {
                    "code": 403,
                    "status": "PERMISSION_DENIED",
                    "message": secret_message,
                    "details": [
                        {
                            "requestId": "safe-request-id",
                            "errors": [
                                {
                                    "errorCode": {
                                        "authorizationError": "USER_PERMISSION_DENIED"
                                    },
                                    "message": secret_message,
                                }
                            ],
                        }
                    ],
                }
            },
        )
        with mock.patch(REQUEST_PATCH, return_value=response), self.assertRaises(
            GoogleApiPermissionError
        ) as caught:
            search_request(
                self.identity,
                self.ACCESS_TOKEN,
                "2345678901",
                "SELECT campaign.id FROM campaign",
            )

        error = caught.exception
        self.assertEqual(error.request_id, "safe-request-id")
        self.assertEqual(error.provider_status, "PERMISSION_DENIED")
        self.assertEqual(error.provider_reason, "USER_PERMISSION_DENIED")
        self.assertEqual(error.operation, "google_ads.search")
        self.assertEqual(error.api_version, "v25")
        self.assertNotIn(secret_message, str(error))
        self.assertTrue(response.closed)

    def test_quota_and_transient_failures_have_bounded_retry_metadata(self):
        quota = FakeResponse(
            status_code=429,
            payload={
                "error": {
                    "status": "RESOURCE_EXHAUSTED",
                    "details": [
                        {
                            "errors": [
                                {
                                    "errorCode": {
                                        "quotaError": "RESOURCE_TEMPORARILY_EXHAUSTED"
                                    }
                                }
                            ]
                        }
                    ],
                }
            },
            headers={"Retry-After": "999999"},
        )
        with mock.patch(REQUEST_PATCH, return_value=quota), self.assertRaises(
            GoogleApiRateLimitError
        ) as caught:
            list_accessible_customers_request(self.identity, self.ACCESS_TOKEN)
        self.assertEqual(caught.exception.retry_after_seconds, 86_400)
        self.assertEqual(
            caught.exception.provider_reason, "RESOURCE_TEMPORARILY_EXHAUSTED"
        )

        transient = FakeResponse(status_code=503, content=b"<html>proxy</html>")
        with mock.patch(REQUEST_PATCH, return_value=transient), self.assertRaises(
            GoogleApiTransientError
        ):
            list_accessible_customers_request(self.identity, self.ACCESS_TOKEN)
        self.assertTrue(transient.closed)

    def test_quota_cooldown_prefers_header_payload_then_reason_specific_fallback(self):
        def quota_response(reason, *, retry_delay="", retry_after=""):
            details = {}
            if retry_delay:
                details["quotaErrorDetails"] = {"retryDelay": retry_delay}
            return FakeResponse(
                status_code=429,
                payload={
                    "error": {
                        "status": "RESOURCE_EXHAUSTED",
                        "details": [
                            {
                                "errors": [
                                    {
                                        "errorCode": {"quotaError": reason},
                                        "details": details,
                                    }
                                ]
                            }
                        ],
                    }
                },
                headers={"Retry-After": retry_after} if retry_after else {},
            )

        cases = (
            (
                quota_response(
                    "EXCESSIVE_SHORT_TERM_QUERY_RESOURCE_CONSUMPTION",
                    retry_delay="420.500s",
                    retry_after="120",
                ),
                120,
            ),
            (
                quota_response(
                    "EXCESSIVE_SHORT_TERM_QUERY_RESOURCE_CONSUMPTION",
                    retry_delay="420.500s",
                ),
                421,
            ),
            (
                quota_response("EXCESSIVE_SHORT_TERM_QUERY_RESOURCE_CONSUMPTION"),
                300,
            ),
            (
                quota_response("EXCESSIVE_LONG_TERM_QUERY_RESOURCE_CONSUMPTION"),
                1_800,
            ),
        )
        for response, expected in cases:
            with self.subTest(expected=expected), mock.patch(
                REQUEST_PATCH, return_value=response
            ), self.assertRaises(GoogleApiRateLimitError) as caught:
                list_accessible_customers_request(self.identity, self.ACCESS_TOKEN)
            self.assertEqual(caught.exception.retry_after_seconds, expected)
            self.assertTrue(response.closed)

    def test_network_exception_does_not_retain_prepared_credentials(self):
        exception = requests.ConnectionError(
            "headers contained %s" % self.DEVELOPER_TOKEN
        )
        with mock.patch(REQUEST_PATCH, side_effect=exception), self.assertRaises(
            GoogleApiTransientError
        ) as caught:
            list_accessible_customers_request(self.identity, self.ACCESS_TOKEN)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(self.DEVELOPER_TOKEN, str(caught.exception))

    def test_oversize_response_and_invalid_identifiers_fail_locally(self):
        response = FakeResponse(
            headers={"Content-Length": str(1024 * 1024)}, payload={}
        )
        with mock.patch(REQUEST_PATCH, return_value=response), self.assertRaises(
            GoogleApiLimitError
        ):
            list_accessible_customers_request(self.identity, self.ACCESS_TOKEN)
        self.assertTrue(response.closed)

        self.assertEqual(normalize_customer_id("123-456-7890"), "1234567890")
        for value in ("123", "customers/1234567890", "123456789x"):
            with self.subTest(value=value), self.assertRaises(GoogleApiError):
                normalize_customer_id(value)

    def test_low_level_transport_revalidates_body_and_header_values(self):
        cases = (
            {"query": ""},
            {"query": "SELECT campaign.id FROM campaign", "page_token": "bad\nvalue"},
            {"query": "x" * (65 * 1024)},
        )
        for values in cases:
            with self.subTest(values=values), mock.patch(
                REQUEST_PATCH
            ) as request_mock, self.assertRaises(GoogleApiError):
                search_request(
                    self.identity,
                    self.ACCESS_TOKEN,
                    "2345678901",
                    **values,
                )
            request_mock.assert_not_called()

        unsafe_identity = GoogleRuntimeIdentity(
            **{
                **self.identity.__dict__,
                "developer_token": "unsafe\ndeveloper-token",
            }
        )
        with mock.patch(REQUEST_PATCH) as request_mock, self.assertRaises(
            GoogleApiError
        ):
            list_accessible_customers_request(unsafe_identity, self.ACCESS_TOKEN)
        request_mock.assert_not_called()

    def test_retry_after_supports_http_dates_without_exceeding_one_day(self):
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(
            retry_after_seconds(
                {"Retry-After": "Tue, 01 Sep 2026 12:05:00 GMT"}, now=now
            ),
            300,
        )
        self.assertEqual(
            retry_after_seconds(
                {"Retry-After": "Tue, 08 Sep 2026 12:00:00 GMT"}, now=now
            ),
            86_400,
        )

    def test_error_metadata_rejects_non_finite_numbers(self):
        error = GoogleApiError(
            "safe",
            retry_after_seconds=float("inf"),
            http_status=float("-inf"),
        )

        self.assertEqual(error.retry_after_seconds, 0)
        self.assertEqual(error.http_status, 0)

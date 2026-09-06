import datetime
import json
import pickle
from unittest import mock

import requests

from odoo.tests.common import SavepointCase

from ..services.credentials import GoogleRuntimeIdentity
from ..services.errors import (
    GoogleApiError,
    GoogleApiLimitError,
    GoogleApiPausedError,
    GoogleApiRateLimitError,
    GoogleApiTransientError,
)
from ..services.oauth import _BoundedGoogleAuthRequest, refresh_access_token
from .common import FakeResponse

REQUEST_PATCH = "odoo.addons.google_api_base.services.oauth.requests.request"


class TestGoogleOAuth(SavepointCase):
    CLIENT_ID = "synthetic-client-id.apps.googleusercontent.com"
    CLIENT_SECRET = "synthetic-client-secret"
    REFRESH_TOKEN = "synthetic-refresh-token"
    DEVELOPER_TOKEN = "synthetic-developer-token"
    ACCESS_TOKEN = "synthetic-short-lived-access-token"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.identity = GoogleRuntimeIdentity(
            active=True,
            api_version="v25",
            oauth_client_id=cls.CLIENT_ID,
            oauth_client_secret=cls.CLIENT_SECRET,
            refresh_token=cls.REFRESH_TOKEN,
            developer_token=cls.DEVELOPER_TOKEN,
            login_customer_id="1234567890",
            public_ref="00000000-0000-4000-8000-000000000001",
            revision=1,
            company_id=1,
        )

    def test_refresh_uses_fixed_endpoint_form_and_private_dto(self):
        response = FakeResponse(
            payload={
                "access_token": self.ACCESS_TOKEN,
                "expires_in": 3600,
                "scope": "https://www.googleapis.com/auth/adwords",
                "token_type": "Bearer",
            }
        )
        with mock.patch(REQUEST_PATCH, return_value=response) as request_mock:
            token = refresh_access_token(self.identity)

        args = request_mock.call_args.args
        kwargs = request_mock.call_args.kwargs
        self.assertEqual(args, ("POST", "https://oauth2.googleapis.com/token"))
        self.assertEqual(
            kwargs["data"],
            {
                "client_id": self.CLIENT_ID,
                "client_secret": self.CLIENT_SECRET,
                "refresh_token": self.REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
        )
        self.assertEqual(kwargs["timeout"], (5, 20))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        self.assertEqual(token.access_token, self.ACCESS_TOKEN)
        self.assertEqual(token.expires_in_seconds, 3600)
        for credential in (
            self.CLIENT_ID,
            self.CLIENT_SECRET,
            self.REFRESH_TOKEN,
            self.ACCESS_TOKEN,
        ):
            self.assertNotIn(credential, repr(token))
        self.assertTrue(response.closed)
        with self.assertRaises(TypeError):
            pickle.dumps(token)

    def test_service_account_transport_is_bounded_and_private(self):
        response = FakeResponse(payload={"access_token": self.ACCESS_TOKEN})
        session = mock.Mock()
        session.request.return_value = response
        transport = _BoundedGoogleAuthRequest(session=session)
        result = transport(
            "https://oauth2.googleapis.com/token",
            method="POST",
            body=b"signed-assertion",
            timeout=120,
            allow_redirects=True,
        )

        self.assertEqual(json.loads(result.data)["access_token"], self.ACCESS_TOKEN)
        self.assertNotIn(self.ACCESS_TOKEN, repr(result))
        self.assertEqual(session.request.call_args.kwargs["timeout"], (5, 20))
        self.assertFalse(session.request.call_args.kwargs["allow_redirects"])
        self.assertTrue(session.request.call_args.kwargs["stream"])
        self.assertTrue(response.closed)

    def test_service_account_transport_preserves_limits_and_retry_taxonomy(self):
        cases = (
            (FakeResponse(content=b" " * (64 * 1024 + 1)), GoogleApiLimitError),
            (FakeResponse(status_code=503, content=b"proxy"), GoogleApiTransientError),
            (FakeResponse(status_code=429, content=b""), GoogleApiRateLimitError),
            (
                FakeResponse(status_code=400, payload={"error": "invalid_grant"}),
                GoogleApiPausedError,
            ),
            (
                FakeResponse(
                    status_code=400, payload={"error": "temporarily_unavailable"}
                ),
                GoogleApiTransientError,
            ),
            (
                FakeResponse(stream_error=requests.ConnectionError(self.ACCESS_TOKEN)),
                GoogleApiTransientError,
            ),
        )
        for response, error_class in cases:
            session = mock.Mock()
            session.request.return_value = response
            with self.subTest(error=error_class), self.assertRaises(
                error_class
            ) as caught:
                _BoundedGoogleAuthRequest(session=session)(
                    "https://oauth2.googleapis.com/token", method="POST"
                )
            session.request.assert_called_once()
            self.assertTrue(response.closed)
            self.assertNotIn(self.ACCESS_TOKEN, str(caught.exception))
            self.assertIsNone(caught.exception.__context__)

    def test_service_account_transport_rejects_other_endpoints_before_network(self):
        session = mock.Mock()
        with self.assertRaises(GoogleApiPausedError):
            _BoundedGoogleAuthRequest(session=session)(
                "https://attacker.invalid/token", method="POST"
            )
        session.request.assert_not_called()

    def test_network_and_transient_provider_failures_are_retryable_without_context(
        self,
    ):
        secret_exception = requests.ConnectionError(
            "request carried %s" % self.REFRESH_TOKEN
        )
        with mock.patch(REQUEST_PATCH, side_effect=secret_exception), self.assertRaises(
            GoogleApiTransientError
        ) as caught:
            refresh_access_token(self.identity)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(self.REFRESH_TOKEN, str(caught.exception))

        for response in (
            FakeResponse(status_code=503, content=b"<html>proxy</html>"),
            FakeResponse(status_code=400, payload={"error": "temporarily_unavailable"}),
        ):
            with self.subTest(status=response.status_code), mock.patch(
                REQUEST_PATCH, return_value=response
            ), self.assertRaises(GoogleApiTransientError):
                refresh_access_token(self.identity)
            self.assertTrue(response.closed)

    def test_authentication_and_quota_have_stable_classes(self):
        paused_responses = (
            FakeResponse(
                status_code=400,
                payload={
                    "error": "invalid_grant",
                    "error_description": self.REFRESH_TOKEN,
                },
            ),
            FakeResponse(status_code=401, content=b""),
        )
        for paused in paused_responses:
            with self.subTest(status=paused.status_code), mock.patch(
                REQUEST_PATCH, return_value=paused
            ), self.assertRaises(GoogleApiPausedError) as caught:
                refresh_access_token(self.identity)
            self.assertNotIn(self.REFRESH_TOKEN, str(caught.exception))
            self.assertTrue(paused.closed)

        limited = FakeResponse(
            status_code=429,
            content=b"",
            headers={"Retry-After": "90", "request-id": "oauth-safe-id"},
        )
        with mock.patch(REQUEST_PATCH, return_value=limited), self.assertRaises(
            GoogleApiRateLimitError
        ) as caught:
            refresh_access_token(self.identity)
        self.assertEqual(caught.exception.retry_after_seconds, 90)
        self.assertEqual(caught.exception.request_id, "oauth-safe-id")
        self.assertEqual(caught.exception.classification, "rate_limited")

    def test_invalid_and_oversize_success_responses_are_rejected(self):
        responses_and_errors = (
            (
                FakeResponse(
                    payload={
                        "access_token": "short",
                        "expires_in": 3600,
                        "token_type": "Bearer",
                    }
                ),
                GoogleApiError,
            ),
            (
                FakeResponse(
                    payload={
                        "access_token": self.ACCESS_TOKEN,
                        "expires_in": 1,
                        "token_type": "Bearer",
                    }
                ),
                GoogleApiError,
            ),
            (
                FakeResponse(
                    payload={
                        "access_token": self.ACCESS_TOKEN,
                        "expires_in": float("inf"),
                        "token_type": "Bearer",
                    }
                ),
                GoogleApiError,
            ),
            (
                FakeResponse(
                    payload={
                        "access_token": "synthetic-token-\ud800",
                        "expires_in": 3600,
                        "token_type": "Bearer",
                    }
                ),
                GoogleApiError,
            ),
            (
                FakeResponse(headers={"Content-Length": str(128 * 1024)}),
                GoogleApiLimitError,
            ),
            (
                FakeResponse(content=b'{"secret":"unterminated'),
                GoogleApiError,
            ),
        )
        for response, error_class in responses_and_errors:
            with self.subTest(error=error_class), mock.patch(
                REQUEST_PATCH, return_value=response
            ), self.assertRaises(error_class) as caught:
                refresh_access_token(self.identity)
            self.assertTrue(response.closed)
            self.assertIsNone(caught.exception.__context__)

    def test_invalid_runtime_contract_is_rejected_before_network(self):
        for identity in (object(), mock.Mock(spec=["ensure_one"])):
            with self.subTest(identity=identity), mock.patch(
                REQUEST_PATCH
            ) as request_mock, self.assertRaises(GoogleApiError):
                refresh_access_token(identity)
            request_mock.assert_not_called()

    def test_service_account_uses_google_auth_scope_and_returns_private_token(self):
        identity = GoogleRuntimeIdentity(
            active=True,
            api_version="v25",
            auth_mode="service_account",
            service_account_info={"type": "service_account", "private_key": "secret"},
            developer_token=self.DEVELOPER_TOKEN,
            login_customer_id="1234567890",
            public_ref="00000000-0000-4000-8000-000000000002",
            revision=1,
            company_id=1,
        )
        credentials = mock.Mock()
        credentials.token = self.ACCESS_TOKEN
        credentials.expiry = datetime.datetime.now(
            datetime.timezone.utc
        ) + datetime.timedelta(seconds=3600)
        with mock.patch(
            "odoo.addons.google_api_base.services.oauth.service_account."
            "Credentials.from_service_account_info",
            return_value=credentials,
        ) as factory, mock.patch(
            "odoo.addons.google_api_base.services.oauth._BoundedGoogleAuthRequest"
        ) as request_class:
            token = refresh_access_token(identity)

        factory.assert_called_once_with(
            dict(identity.service_account_info),
            scopes=("https://www.googleapis.com/auth/adwords",),
        )
        credentials.refresh.assert_called_once_with(request_class.return_value)
        self.assertEqual(token.access_token, self.ACCESS_TOKEN)
        self.assertGreaterEqual(token.expires_in_seconds, 3598)
        self.assertNotIn(self.ACCESS_TOKEN, repr(token))

    def test_service_account_errors_are_sanitized_and_classified(self):
        identity = GoogleRuntimeIdentity(
            active=True,
            api_version="v25",
            auth_mode="service_account",
            service_account_info={"type": "service_account", "private_key": "secret"},
            developer_token=self.DEVELOPER_TOKEN,
            login_customer_id="",
            public_ref="00000000-0000-4000-8000-000000000003",
            revision=1,
            company_id=1,
        )
        secret_error = ValueError("private secret leaked")
        with mock.patch(
            "odoo.addons.google_api_base.services.oauth.service_account."
            "Credentials.from_service_account_info",
            side_effect=secret_error,
        ), self.assertRaises(GoogleApiPausedError) as caught:
            refresh_access_token(identity)
        self.assertNotIn("private secret", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

        credentials = mock.Mock()
        credentials.token = "synthetic-token-\ud800"
        credentials.expiry = datetime.datetime.now(
            datetime.timezone.utc
        ) + datetime.timedelta(seconds=3600)
        with mock.patch(
            "odoo.addons.google_api_base.services.oauth.service_account."
            "Credentials.from_service_account_info",
            return_value=credentials,
        ), self.assertRaises(GoogleApiError):
            refresh_access_token(identity)

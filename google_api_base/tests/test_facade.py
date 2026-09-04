from unittest import mock

from odoo.tests.common import SavepointCase

from ..services.contracts import GoogleOAuthToken
from ..services.credentials import GoogleRuntimeIdentity
from ..services.errors import GoogleApiError, GoogleApiLimitError
from ..services.facade import GoogleAdsFacade

OAUTH_PATCH = "odoo.addons.google_api_base.services.facade.refresh_access_token"
LIST_PATCH = (
    "odoo.addons.google_api_base.services.facade.list_accessible_customers_request"
)
SEARCH_PATCH = "odoo.addons.google_api_base.services.facade.search_request"


class TestGoogleAdsFacade(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.identity = GoogleRuntimeIdentity(
            active=True,
            api_version="v25",
            oauth_client_id="synthetic-client-id.apps.googleusercontent.com",
            oauth_client_secret="synthetic-client-secret",
            refresh_token="synthetic-refresh-token",
            developer_token="synthetic-developer-token",
            login_customer_id="1234567890",
            public_ref="00000000-0000-4000-8000-000000000001",
            revision=1,
            company_id=1,
        )
        cls.oauth_token = GoogleOAuthToken(
            access_token="synthetic-short-lived-access-token",
            expires_in_seconds=3600,
        )
        cls.facade = GoogleAdsFacade(cls.identity)

    def test_discovery_returns_a_small_immutable_technical_dto(self):
        with mock.patch(OAUTH_PATCH, return_value=self.oauth_token), mock.patch(
            LIST_PATCH,
            return_value=(
                {
                    "resourceNames": [
                        "customers/1234567890",
                        "customers/2345678901",
                    ]
                },
                "discovery-request",
            ),
        ):
            result = self.facade.list_accessible_customers()

        self.assertEqual(
            result.resource_names,
            ("customers/1234567890", "customers/2345678901"),
        )
        self.assertEqual(result.request_id, "discovery-request")
        with self.assertRaises(AttributeError):
            result.resource_names = ()

    def test_discovery_rejects_duplicates_and_malformed_resource_names(self):
        payloads = (
            {"resourceNames": ["customers/123"]},
            {
                "resourceNames": [
                    "customers/1234567890",
                    "customers/1234567890",
                ]
            },
            {"resourceNames": "customers/1234567890"},
        )
        for payload in payloads:
            with self.subTest(payload=payload), mock.patch(
                OAUTH_PATCH, return_value=self.oauth_token
            ), mock.patch(LIST_PATCH, return_value=(payload, "")), self.assertRaises(
                GoogleApiError
            ):
                self.facade.list_accessible_customers()

    def test_search_page_detaches_rows_and_keeps_token_out_of_repr(self):
        payload = {
            "results": [{"campaign": {"id": "123"}}],
            "nextPageToken": "opaque-next-token",
            "fieldMask": "campaign.id",
            "totalResultsCount": "2",
        }
        with mock.patch(OAUTH_PATCH, return_value=self.oauth_token), mock.patch(
            SEARCH_PATCH, return_value=(payload, "search-request")
        ):
            page = self.facade.search_page(
                "2345678901", "SELECT campaign.id FROM campaign"
            )
        payload["results"][0]["campaign"]["id"] = "changed"

        self.assertEqual(page.results[0]["campaign"]["id"], "123")
        self.assertEqual(page.next_page_token, "opaque-next-token")
        self.assertEqual(page.total_results_count, 2)
        self.assertNotIn("opaque-next-token", repr(page))

    def test_search_page_rejects_non_finite_total(self):
        payloads = (
            {"results": [], "totalResultsCount": float("inf")},
            {"results": [], "fieldMask": "invalid\ud800mask"},
            {"results": [], "fieldMask": "invalid\x7fmask"},
        )
        for payload in payloads:
            with self.subTest(payload=repr(payload)), mock.patch(
                OAUTH_PATCH, return_value=self.oauth_token
            ), mock.patch(
                SEARCH_PATCH, return_value=(payload, "search-request")
            ), self.assertRaises(
                GoogleApiError
            ):
                self.facade.search_page(
                    "2345678901", "SELECT campaign.id FROM campaign"
                )

    def test_bounded_iterator_reuses_oauth_token_and_advances_opaque_cursor(self):
        pages = [
            (
                {"results": [{"campaign": {"id": "1"}}], "nextPageToken": "p2"},
                "r1",
            ),
            ({"results": [{"campaign": {"id": "2"}}]}, "r2"),
        ]
        with mock.patch(
            OAUTH_PATCH, return_value=self.oauth_token
        ) as oauth_mock, mock.patch(SEARCH_PATCH, side_effect=pages) as search_mock:
            results = list(
                self.facade.iter_search_pages(
                    "2345678901",
                    "SELECT campaign.id FROM campaign",
                    max_pages=2,
                    max_rows=2,
                )
            )

        self.assertEqual(len(results), 2)
        oauth_mock.assert_called_once_with(self.identity)
        self.assertEqual(search_mock.call_args_list[0].kwargs["page_token"], "")
        self.assertEqual(search_mock.call_args_list[1].kwargs["page_token"], "p2")

    def test_iterator_rejects_cursor_loops_page_ceiling_and_row_ceiling(self):
        cases = (
            (
                [
                    ({"results": [], "nextPageToken": "repeat"}, "r1"),
                    ({"results": [], "nextPageToken": "repeat"}, "r2"),
                ],
                {"max_pages": 3, "max_rows": 3},
                GoogleApiError,
            ),
            (
                [({"results": [], "nextPageToken": "more"}, "r1")],
                {"max_pages": 1, "max_rows": 3},
                GoogleApiLimitError,
            ),
            (
                [({"results": [{}, {}]}, "r1")],
                {"max_pages": 1, "max_rows": 1},
                GoogleApiLimitError,
            ),
        )
        for responses, limits, error_class in cases:
            with self.subTest(error=error_class), mock.patch(
                OAUTH_PATCH, return_value=self.oauth_token
            ), mock.patch(SEARCH_PATCH, side_effect=responses), self.assertRaises(
                error_class
            ):
                list(
                    self.facade.iter_search_pages(
                        "2345678901",
                        "SELECT campaign.id FROM campaign",
                        **limits,
                    )
                )

    def test_query_page_token_and_bounds_are_validated_before_io(self):
        invalid_calls = (
            lambda: self.facade.search_page(
                "invalid-customer", "SELECT campaign.id FROM campaign"
            ),
            lambda: self.facade.search_page("2345678901", ""),
            lambda: self.facade.search_page(
                "2345678901",
                "SELECT campaign.id FROM campaign",
                page_token="x" * 5000,
            ),
            lambda: list(
                self.facade.iter_search_pages(
                    "2345678901",
                    "SELECT campaign.id FROM campaign",
                    max_pages=0,
                )
            ),
        )
        for call in invalid_calls:
            with self.subTest(call=call), mock.patch(
                OAUTH_PATCH
            ) as oauth_mock, mock.patch(SEARCH_PATCH) as search_mock, self.assertRaises(
                GoogleApiError
            ):
                call()
            oauth_mock.assert_not_called()
            search_mock.assert_not_called()

    def test_iterator_enforces_aggregate_elapsed_time_and_token_lifetime(self):
        with mock.patch(
            OAUTH_PATCH,
            return_value=GoogleOAuthToken(
                access_token="synthetic-short-lived-access-token",
                expires_in_seconds=60,
            ),
        ), mock.patch(
            SEARCH_PATCH,
            return_value=({"results": [], "nextPageToken": "more"}, "r1"),
        ) as search_mock, mock.patch(
            "odoo.addons.google_api_base.services.facade.time.monotonic",
            side_effect=(0, 0, 31),
        ), self.assertRaises(
            GoogleApiLimitError
        ):
            list(
                self.facade.iter_search_pages(
                    "2345678901",
                    "SELECT campaign.id FROM campaign",
                    max_pages=2,
                    max_elapsed_seconds=240,
                )
            )
        search_mock.assert_called_once()

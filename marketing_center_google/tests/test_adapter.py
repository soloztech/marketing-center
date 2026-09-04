from types import SimpleNamespace
from unittest.mock import patch

from odoo.tests.common import SavepointCase

from odoo.addons.google_api_base.services.credentials import GoogleRuntimeIdentity
from odoo.addons.google_api_base.services.errors import (
    GoogleApiError,
    GoogleApiLimitError,
    GoogleApiPermissionError,
)

from ..services.adapter import GoogleMarketingReadAdapter
from .common import create_google_profile

_FACADE_PATH = "odoo.addons.marketing_center_google.services.adapter.GoogleAdsFacade"
_RUNTIME_PATH = (
    "odoo.addons.google_api_base.models.google_identity."
    "GoogleApiIdentity._resolve_runtime"
)


def _client(
    customer_id,
    *,
    level,
    manager,
    name=None,
    status="ENABLED",
):
    return {
        "customerClient": {
            "clientCustomer": "customers/%s" % customer_id,
            "level": level,
            "manager": manager,
            "descriptiveName": name or "Customer %s" % customer_id,
            "currencyCode": "BRL",
            "timeZone": "America/Sao_Paulo",
            "status": status,
            "testAccount": False,
            "hidden": False,
        }
    }


def _self_customer(customer_id, *, manager, name=None):
    return {
        "customer": {
            "resourceName": "customers/%s" % customer_id,
            "manager": manager,
            "descriptiveName": name or "Customer %s" % customer_id,
            "currencyCode": "BRL",
            "timeZone": "America/Sao_Paulo",
            "status": "ENABLED",
            "testAccount": False,
        }
    }


class TestGoogleMarketingReadAdapter(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.identity, cls.profile = create_google_profile(cls.env)

    def _adapter(
        self,
        facade=None,
        *,
        login_customer_id="",
        binding_login_customer_id=None,
        facade_factory=None,
    ):
        runtime = GoogleRuntimeIdentity(
            active=True,
            api_version="v25",
            developer_token="developer-token",
            company_id=self.env.company.id,
            login_customer_id=login_customer_id,
            public_ref="00000000-0000-0000-0000-000000000001",
            revision=self.profile.identity_revision,
        )
        runtime_patch = patch(_RUNTIME_PATH, return_value=runtime)
        facade_patch = patch(
            _FACADE_PATH,
            side_effect=facade_factory,
            return_value=facade,
        )
        runtime_patch.start()
        facade_patch.start()
        self.addCleanup(runtime_patch.stop)
        self.addCleanup(facade_patch.stop)
        return GoogleMarketingReadAdapter(
            self.profile,
            expected_profile_revision=self.profile.profile_revision,
            expected_identity_revision=self.profile.identity_revision,
            login_customer_id=binding_login_customer_id,
        )

    def test_recursive_customer_client_discovery_is_bounded_and_deduplicated(self):
        facade = SimpleNamespace()
        facade.list_accessible_customers = lambda: SimpleNamespace(
            resource_names=("customers/1111111111",), request_id="request-root"
        )
        pages = {
            "1111111111": (
                _client("1111111111", level=0, manager=True),
                _client("2222222222", level=1, manager=True),
                _client("3333333333", level=1, manager=False),
            ),
            "2222222222": (
                _client("2222222222", level=0, manager=True),
                _client("4444444444", level=1, manager=False),
            ),
        }
        calls = []

        def iter_pages(customer_id, query, **bounds):
            calls.append((customer_id, query, bounds))
            yield SimpleNamespace(results=pages[customer_id])

        facade.iter_search_pages = iter_pages
        facade.search_page = lambda customer_id, _query: SimpleNamespace(
            results=(_self_customer(customer_id, manager=True),)
        )
        result = self._adapter(facade).discover_customers(
            max_roots=1,
            max_depth=2,
            max_customers=4,
            max_pages_per_manager=2,
        )
        self.assertEqual(
            [customer.customer_id for customer in result.customers],
            ["1111111111", "2222222222", "3333333333", "4444444444"],
        )
        self.assertEqual([call[0] for call in calls], ["1111111111", "2222222222"])
        self.assertEqual(result.request_count, 4)
        self.assertTrue(all("customer_client" in call[1] for call in calls))
        self.assertTrue(
            all(
                "customer_client.id" in call[1].partition(" FROM ")[0]
                and "ORDER BY customer_client.level, customer_client.id" in call[1]
                for call in calls
            )
        )
        self.assertTrue(all(call[2]["max_pages"] == 2 for call in calls))

    def test_login_customer_scopes_one_accessible_hierarchy(self):
        facade = SimpleNamespace()
        facade.list_accessible_customers = lambda: SimpleNamespace(
            resource_names=("customers/1111111111", "customers/2222222222"),
            request_id="request-root",
        )
        observed = []

        def iter_pages(customer_id, _query, **_bounds):
            observed.append(customer_id)
            yield SimpleNamespace(
                results=(_client(customer_id, level=0, manager=False),)
            )

        facade.iter_search_pages = iter_pages
        result = self._adapter(
            facade, login_customer_id="2222222222"
        ).discover_customers()
        self.assertEqual(result.root_count, 1)
        self.assertEqual(observed, ["2222222222"])

    def test_unknown_login_customer_and_depth_overflow_fail_closed(self):
        facade = SimpleNamespace()
        facade.list_accessible_customers = lambda: SimpleNamespace(
            resource_names=("customers/1111111111",), request_id="request-root"
        )
        with self.assertRaises(GoogleApiPermissionError):
            self._adapter(facade, login_customer_id="2222222222").discover_customers()

        facade.iter_search_pages = lambda *_args, **_kwargs: iter(
            (
                SimpleNamespace(
                    results=(
                        _client("1111111111", level=0, manager=True),
                        _client("2222222222", level=1, manager=True),
                    )
                ),
            )
        )
        facade.search_page = lambda customer_id, _query: SimpleNamespace(
            results=(_self_customer(customer_id, manager=True),)
        )
        result = self._adapter(facade).discover_customers(max_depth=1)
        self.assertEqual(len(result.customers), 2)

    def test_validate_rejects_identity_without_accessible_roots(self):
        facade = SimpleNamespace()
        facade.list_accessible_customers = lambda: SimpleNamespace(
            resource_names=(), request_id="request-root"
        )
        with self.assertRaises(GoogleApiPermissionError):
            self._adapter(facade).validate()

    def test_dynamic_multi_root_uses_each_manager_as_its_tree_login(self):
        calls = []
        self_rows = {
            "1111111111": _self_customer("1111111111", manager=True),
            "2222222222": _self_customer("2222222222", manager=True),
        }
        hierarchy = {
            ("1111111111", "1111111111"): (
                _client("1111111111", level=0, manager=True),
                _client("3333333333", level=1, manager=True),
                _client("4444444444", level=1, manager=False),
            ),
            ("1111111111", "3333333333"): (
                _client("3333333333", level=0, manager=True),
                _client("5555555555", level=1, manager=False),
            ),
            ("2222222222", "2222222222"): (
                _client("2222222222", level=0, manager=True),
                _client("4444444444", level=1, manager=False),
            ),
        }

        def factory(runtime):
            login = runtime.login_customer_id
            facade = SimpleNamespace()
            facade.list_accessible_customers = lambda: SimpleNamespace(
                resource_names=("customers/2222222222", "customers/1111111111"),
                request_id="request-root",
            )

            def search_page(customer_id, query):
                calls.append(("self", login, customer_id))
                self.assertFalse(login)
                self.assertIn("FROM customer", query)
                return SimpleNamespace(results=(self_rows[customer_id],))

            def iter_pages(customer_id, _query, **_bounds):
                calls.append(("tree", login, customer_id))
                yield SimpleNamespace(results=hierarchy[(login, customer_id)])

            facade.search_page = search_page
            facade.iter_search_pages = iter_pages
            return facade

        result = self._adapter(facade_factory=factory).discover_customers(
            max_roots=2,
            max_depth=2,
            max_customers=5,
        )
        leaf = next(
            customer
            for customer in result.customers
            if customer.customer_id == "4444444444"
        )
        self.assertEqual(leaf.access_login_customer_id, "1111111111")
        self.assertEqual(
            [call for call in calls if call[0] == "tree"],
            [
                ("tree", "1111111111", "1111111111"),
                ("tree", "2222222222", "2222222222"),
                ("tree", "1111111111", "3333333333"),
            ],
        )

    def test_direct_leaf_uses_no_login_header_and_needs_no_hierarchy(self):
        calls = []

        def factory(runtime):
            self.assertFalse(runtime.login_customer_id)
            facade = SimpleNamespace()
            facade.list_accessible_customers = lambda: SimpleNamespace(
                resource_names=("customers/5555555555",),
                request_id="request-root",
            )
            facade.search_page = lambda customer_id, _query: (
                calls.append(customer_id)
                or SimpleNamespace(
                    results=(_self_customer(customer_id, manager=False),)
                )
            )
            facade.iter_search_pages = lambda *_args, **_kwargs: self.fail(
                "A directly accessible leaf must not expand a manager hierarchy"
            )
            return facade

        result = self._adapter(facade_factory=factory).discover_customers()
        self.assertEqual(calls, ["5555555555"])
        self.assertEqual(len(result.customers), 1)
        self.assertFalse(result.customers[0].access_login_customer_id)

    def test_fixed_login_precedes_dynamic_tree_selection_and_leaf_fallback(self):
        calls = []
        facade = SimpleNamespace()
        facade.list_accessible_customers = lambda: SimpleNamespace(
            resource_names=("customers/6666666666", "customers/7777777777"),
            request_id="request-root",
        )

        def iter_pages(customer_id, _query, **_bounds):
            calls.append(("tree", customer_id))
            yield SimpleNamespace(results=())

        facade.iter_search_pages = iter_pages
        facade.search_page = lambda customer_id, _query: (
            calls.append(("self", customer_id))
            or SimpleNamespace(results=(_self_customer(customer_id, manager=False),))
        )
        result = self._adapter(
            facade,
            login_customer_id="7777777777",
        ).discover_customers()
        self.assertEqual(calls, [("tree", "7777777777"), ("self", "7777777777")])
        self.assertEqual(result.root_count, 1)
        self.assertEqual(
            result.customers[0].access_login_customer_id,
            "7777777777",
        )

    def test_fixed_identity_rejects_a_stale_effective_login_binding(self):
        with self.assertRaises(GoogleApiError):
            self._adapter(
                SimpleNamespace(),
                login_customer_id="8888888888",
                binding_login_customer_id="9999999999",
            )

    def test_profile_and_identity_fences_precede_facade_creation(self):
        stale_revisions = (
            (
                "profile",
                self.profile.profile_revision + 1,
                self.profile.identity_revision,
            ),
            (
                "identity",
                self.profile.profile_revision,
                self.profile.identity_revision + 1,
            ),
        )
        for (
            fence,
            expected_profile_revision,
            expected_identity_revision,
        ) in stale_revisions:
            with self.subTest(fence=fence), patch(_RUNTIME_PATH) as runtime, patch(
                _FACADE_PATH
            ) as facade:
                with self.assertRaises(GoogleApiError):
                    GoogleMarketingReadAdapter(
                        self.profile,
                        expected_profile_revision=expected_profile_revision,
                        expected_identity_revision=expected_identity_revision,
                    )
                runtime.assert_not_called()
                facade.assert_not_called()

        facade = SimpleNamespace()
        with self.assertRaises(GoogleApiLimitError):
            self._adapter(facade).discover_customers(max_customers=0)

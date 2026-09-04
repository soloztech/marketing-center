import dataclasses
import re

from odoo.addons.google_api_base.services.errors import (
    GoogleApiError,
    GoogleApiLimitError,
    GoogleApiPermissionError,
)
from odoo.addons.google_api_base.services.facade import GoogleAdsFacade
from odoo.addons.google_api_base.services.tokens import (
    GOOGLE_API_RUNTIME_CONTEXT_KEY,
    GOOGLE_API_RUNTIME_TOKEN,
)

from .catalog import (
    google_catalog_spec,
    normalize_customer_id,
    normalize_google_catalog_page,
)
from .observability import (
    google_change_query,
    google_diagnostic_spec,
    normalize_google_change_page,
    normalize_google_diagnostic_page,
)
from .performance import google_performance_query, normalize_google_performance_page

_CUSTOMER_RESOURCE_RE = re.compile(r"^customers/([0-9]{10})$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_STATUS_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_MAX_DISCOVERY_ROOTS = 50
_MAX_DISCOVERY_DEPTH = 5
_MAX_DISCOVERY_CUSTOMERS = 500
_MAX_DISCOVERY_PAGES = 4

_CUSTOMER_HIERARCHY_QUERY = " ".join(
    """
    SELECT customer_client.client_customer, customer_client.id,
           customer_client.level,
           customer_client.manager, customer_client.descriptive_name,
           customer_client.currency_code, customer_client.time_zone,
           customer_client.status, customer_client.test_account,
           customer_client.hidden
      FROM customer_client
     WHERE customer_client.level <= 1
     ORDER BY customer_client.level, customer_client.id
    """.split()
)

_CUSTOMER_SELF_QUERY = " ".join(
    """
    SELECT customer.resource_name, customer.descriptive_name,
           customer.currency_code, customer.time_zone, customer.status,
           customer.manager, customer.test_account
      FROM customer
     LIMIT 1
    """.split()
)


@dataclasses.dataclass(frozen=True)
class GoogleDiscoveredCustomer:
    resource_name: str
    customer_id: str
    name: str
    currency: str
    timezone: str
    status: str
    manager: bool
    test_account: bool
    hidden: bool
    depth: int
    access_login_customer_id: str = ""


@dataclasses.dataclass(frozen=True)
class GoogleDiscoveryResult:
    customers: tuple
    root_count: int
    request_count: int


class GoogleMarketingReadAdapter:
    """Marketing-specific allowlist over the domain-neutral Google facade."""

    def __init__(
        self,
        profile,
        *,
        expected_profile_revision,
        expected_identity_revision,
        login_customer_id=None,
    ):
        profile.ensure_one()
        if profile.profile_revision != expected_profile_revision:
            raise GoogleApiError("Google marketing profile changed")
        if profile.identity_revision != expected_identity_revision:
            raise GoogleApiError("Google identity binding changed")
        identity = profile.google_identity_id.with_context(
            **{GOOGLE_API_RUNTIME_CONTEXT_KEY: GOOGLE_API_RUNTIME_TOKEN}
        )
        runtime = identity._resolve_runtime(
            expected_revision=expected_identity_revision
        )
        if runtime.company_id != profile.company_id.id:
            raise GoogleApiError("Google identity company is inconsistent")
        self._runtime = runtime
        self._identity_login_customer_id = (
            normalize_customer_id(runtime.login_customer_id)
            if runtime.login_customer_id
            else ""
        )
        self._facades = {
            self._identity_login_customer_id: GoogleAdsFacade(runtime),
        }
        if login_customer_id is None:
            effective_login_customer_id = self._identity_login_customer_id
        else:
            effective_login_customer_id = (
                normalize_customer_id(login_customer_id) if login_customer_id else ""
            )
            if (
                self._identity_login_customer_id
                and effective_login_customer_id != self._identity_login_customer_id
            ):
                raise GoogleApiError("Google Ads login customer binding changed")
        self._facade = self._facade_for_login_customer(effective_login_customer_id)

    def validate(self):
        accessible = self._facade.list_accessible_customers()
        roots = self._discovery_roots(accessible.resource_names)
        if not roots:
            raise GoogleApiPermissionError(
                "Google Ads identity has no accessible customer roots"
            )
        return {
            "accessible_root_count": len(roots),
            "request_id": accessible.request_id,
        }

    def discover_customers(
        self,
        *,
        max_roots=_MAX_DISCOVERY_ROOTS,
        max_depth=_MAX_DISCOVERY_DEPTH,
        max_customers=_MAX_DISCOVERY_CUSTOMERS,
        max_pages_per_manager=_MAX_DISCOVERY_PAGES,
    ):
        max_roots = _bound(max_roots, "root", _MAX_DISCOVERY_ROOTS)
        max_depth = _bound(max_depth, "depth", _MAX_DISCOVERY_DEPTH)
        max_customers = _bound(max_customers, "customer", _MAX_DISCOVERY_CUSTOMERS)
        max_pages_per_manager = _bound(
            max_pages_per_manager, "page", _MAX_DISCOVERY_PAGES
        )
        accessible = self._facade.list_accessible_customers()
        roots = self._discovery_roots(accessible.resource_names)
        if len(roots) > max_roots:
            raise GoogleApiLimitError("Google Ads discovery root limit was exceeded")
        queue, observed, root_request_count = self._seed_discovery_roots(
            roots,
            max_customers=max_customers,
        )
        expanded = set()
        request_count = 1 + root_request_count
        while queue:
            customer_id, depth, ancestors, login_customer_id, root_id = queue.pop(0)
            if depth > max_depth:
                raise GoogleApiLimitError(
                    "Google Ads discovery depth limit was exceeded"
                )
            if customer_id in ancestors:
                raise GoogleApiError("Google Ads customer hierarchy contains a cycle")
            expansion_key = (root_id, customer_id)
            if expansion_key in expanded:
                continue
            expanded.add(expansion_key)
            facade = self._facade_for_login_customer(login_customer_id)
            descriptors, page_count = self._hierarchy_descriptors(
                facade,
                customer_id,
                depth,
                root_id,
                login_customer_id,
                max_pages=max_pages_per_manager,
                max_rows=max_customers,
            )
            request_count += page_count
            for item in descriptors:
                if item.depth > max_depth:
                    raise GoogleApiLimitError(
                        "Google Ads discovery depth limit was exceeded"
                    )
                _observe_customer(observed, item, root_id)
                if len(observed) > max_customers:
                    raise GoogleApiLimitError(
                        "Google Ads discovery customer limit was exceeded"
                    )
                if (
                    item.manager
                    and item.customer_id != customer_id
                    and item.depth < max_depth
                ):
                    queue.append(
                        (
                            item.customer_id,
                            item.depth,
                            ancestors | {customer_id},
                            login_customer_id,
                            root_id,
                        )
                    )
        return GoogleDiscoveryResult(
            customers=tuple(
                sorted(
                    (value[0] for value in observed.values()),
                    key=lambda item: (item.depth, item.customer_id),
                )
            ),
            root_count=len(roots),
            request_count=request_count,
        )

    def _seed_discovery_roots(self, roots, *, max_customers):
        if self._identity_login_customer_id:
            return (
                [
                    (
                        root,
                        0,
                        frozenset(),
                        self._identity_login_customer_id,
                        root,
                    )
                    for root in roots
                ],
                {},
                0,
            )
        queue = []
        observed = {}
        for root in roots:
            page = self._facade_for_login_customer("").search_page(
                root,
                _CUSTOMER_SELF_QUERY,
            )
            customer = _customer_self(
                page.results,
                query_depth=0,
                access_login_customer_id="",
            )
            if customer.customer_id != root:
                raise GoogleApiError(
                    "Google Ads directly accessible customer is inconsistent"
                )
            if customer.manager:
                queue.append((root, 0, frozenset(), root, root))
            else:
                _observe_customer(observed, customer, root)
            if len(observed) > max_customers:
                raise GoogleApiLimitError(
                    "Google Ads discovery customer limit was exceeded"
                )
        return queue, observed, len(roots)

    def _hierarchy_descriptors(
        self,
        facade,
        customer_id,
        depth,
        root_id,
        login_customer_id,
        *,
        max_pages,
        max_rows,
    ):
        rows = []
        page_count = 0
        for page in facade.iter_search_pages(
            customer_id,
            _CUSTOMER_HIERARCHY_QUERY,
            max_pages=max_pages,
            max_rows=max_rows,
            max_elapsed_seconds=120,
        ):
            page_count += 1
            rows.extend(page.results)
        descriptors = tuple(
            _customer_client(
                row,
                query_depth=depth,
                access_login_customer_id=login_customer_id,
            )
            for row in rows
        )
        if any(item.customer_id == customer_id for item in descriptors):
            return descriptors, page_count
        if depth or customer_id != root_id:
            raise GoogleApiError("Google Ads customer hierarchy is incomplete")
        page = facade.search_page(customer_id, _CUSTOMER_SELF_QUERY)
        descriptor = _customer_self(
            page.results,
            query_depth=depth,
            access_login_customer_id=login_customer_id,
        )
        return (descriptor,) + descriptors, page_count + 1

    def _facade_for_login_customer(self, login_customer_id):
        login_customer_id = (
            normalize_customer_id(login_customer_id) if login_customer_id else ""
        )
        facade = self._facades.get(login_customer_id)
        if facade is not None:
            return facade
        try:
            runtime = dataclasses.replace(
                self._runtime,
                login_customer_id=login_customer_id,
            )
        except (TypeError, ValueError):
            raise GoogleApiError(
                "Google runtime identity cannot be scoped safely"
            ) from None
        facade = GoogleAdsFacade(runtime)
        self._facades[login_customer_id] = facade
        return facade

    def fetch_catalog_pages(
        self,
        customer_id,
        stage,
        *,
        page_token="",
        reporting_context_hash,
        observed_at=None,
    ):
        customer_id = normalize_customer_id(customer_id)
        page = self._facade.search_page(
            customer_id,
            google_catalog_spec(stage).query,
            page_token=page_token,
        )
        return normalize_google_catalog_page(
            customer_id,
            stage,
            page,
            current_page_token=page_token,
            reporting_context_hash=reporting_context_hash,
            observed_at=observed_at,
        )

    def fetch_performance_pages(
        self,
        customer_id,
        grain,
        *,
        date_from,
        date_to,
        currency,
        report_timezone,
        page_token="",
        reporting_context_hash,
        observed_at=None,
    ):
        customer_id = normalize_customer_id(customer_id)
        query = google_performance_query(grain, date_from, date_to)
        page = self._facade.search_page(
            customer_id,
            query,
            page_token=page_token,
        )
        return normalize_google_performance_page(
            customer_id,
            grain,
            page,
            current_page_token=page_token,
            date_from=date_from,
            date_to=date_to,
            currency=currency,
            report_timezone=report_timezone,
            reporting_context_hash=reporting_context_hash,
            observed_at=observed_at,
        )

    def fetch_change_pages(
        self,
        customer_id,
        *,
        local_date,
        report_timezone,
        page_token="",
        reporting_context_hash,
        observed_at=None,
    ):
        customer_id = normalize_customer_id(customer_id)
        page = self._facade.search_page(
            customer_id,
            google_change_query(local_date),
            page_token=page_token,
        )
        return normalize_google_change_page(
            customer_id,
            page,
            current_page_token=page_token,
            local_date=local_date,
            report_timezone=report_timezone,
            reporting_context_hash=reporting_context_hash,
            observed_at=observed_at,
        )

    def fetch_diagnostic_pages(
        self,
        customer_id,
        stage,
        *,
        page_token="",
        reporting_context_hash,
        observed_at=None,
    ):
        customer_id = normalize_customer_id(customer_id)
        spec = google_diagnostic_spec(stage)
        page = self._facade.search_page(
            customer_id,
            spec.query,
            page_token=page_token,
        )
        return normalize_google_diagnostic_page(
            customer_id,
            stage,
            page,
            current_page_token=page_token,
            reporting_context_hash=reporting_context_hash,
            observed_at=observed_at,
        )

    def _discovery_roots(self, resource_names):
        if not isinstance(resource_names, tuple):
            raise GoogleApiError("Google Ads accessible customers are invalid")
        roots = tuple(_customer_resource(value) for value in resource_names)
        login_customer_id = (
            normalize_customer_id(self._runtime.login_customer_id)
            if (self._runtime.login_customer_id)
            else ""
        )
        if login_customer_id:
            if login_customer_id not in roots:
                raise GoogleApiPermissionError(
                    "Google Ads login customer is not directly accessible"
                )
            return (login_customer_id,)
        return tuple(sorted(roots))


def _customer_client(row, *, query_depth, access_login_customer_id=""):
    if not isinstance(row, dict) or not isinstance(row.get("customerClient"), dict):
        raise GoogleApiError("Google Ads customer hierarchy row is invalid")
    value = row["customerClient"]
    resource_name = value.get("clientCustomer")
    customer_id = _customer_resource(resource_name)
    level = _nonnegative_int(value.get("level"), "customer level", 1)
    manager = _boolean(value.get("manager"), "manager flag")
    test_account = _boolean(value.get("testAccount", False), "test-account flag")
    hidden = _boolean(value.get("hidden", False), "hidden flag")
    name = _text(value.get("descriptiveName"), "customer name", 1024, required=False)
    if not name:
        name = "Google Ads %s" % customer_id
    currency = _text(value.get("currencyCode"), "customer currency", 3).upper()
    if not _CURRENCY_RE.fullmatch(currency):
        raise GoogleApiError("Google Ads customer currency is invalid")
    timezone = _text(value.get("timeZone"), "customer timezone", 64)
    status = _text(value.get("status"), "customer status", 128).upper()
    if not _STATUS_RE.fullmatch(status):
        raise GoogleApiError("Google Ads customer status is invalid")
    return GoogleDiscoveredCustomer(
        resource_name=resource_name,
        customer_id=customer_id,
        name=name,
        currency=currency,
        timezone=timezone,
        status=status.lower(),
        manager=manager,
        test_account=test_account,
        hidden=hidden,
        depth=query_depth + level,
        access_login_customer_id=access_login_customer_id,
    )


def _customer_self(rows, *, query_depth, access_login_customer_id):
    if not isinstance(rows, tuple) or len(rows) != 1:
        raise GoogleApiError("Google Ads directly accessible customer is invalid")
    row = rows[0]
    if not isinstance(row, dict) or not isinstance(row.get("customer"), dict):
        raise GoogleApiError("Google Ads directly accessible customer is invalid")
    value = row["customer"]
    resource_name = value.get("resourceName")
    customer_id = _customer_resource(resource_name)
    manager = _boolean(value.get("manager"), "manager flag")
    test_account = _boolean(value.get("testAccount", False), "test-account flag")
    name = _text(value.get("descriptiveName"), "customer name", 1024, required=False)
    if not name:
        name = "Google Ads %s" % customer_id
    currency = _text(value.get("currencyCode"), "customer currency", 3).upper()
    if not _CURRENCY_RE.fullmatch(currency):
        raise GoogleApiError("Google Ads customer currency is invalid")
    timezone = _text(value.get("timeZone"), "customer timezone", 64)
    status = _text(value.get("status"), "customer status", 128).upper()
    if not _STATUS_RE.fullmatch(status):
        raise GoogleApiError("Google Ads customer status is invalid")
    return GoogleDiscoveredCustomer(
        resource_name=resource_name,
        customer_id=customer_id,
        name=name,
        currency=currency,
        timezone=timezone,
        status=status.lower(),
        manager=manager,
        test_account=test_account,
        hidden=False,
        depth=query_depth,
        access_login_customer_id=access_login_customer_id,
    )


def _observe_customer(observed, customer, root_id):
    prior = observed.get(customer.customer_id)
    selection_key = (customer.depth, root_id)
    if prior and prior[1] == selection_key:
        if _customer_identity(prior[0]) != _customer_identity(customer):
            raise GoogleApiError("Google Ads customer hierarchy is inconsistent")
        return
    if not prior or selection_key < prior[1]:
        observed[customer.customer_id] = (customer, selection_key)


def _customer_identity(value):
    return (
        value.resource_name,
        value.name,
        value.currency,
        value.timezone,
        value.status,
        value.manager,
        value.test_account,
        value.hidden,
    )


def _customer_resource(value):
    if not isinstance(value, str):
        raise GoogleApiError("Google Ads customer resource is invalid")
    match = _CUSTOMER_RESOURCE_RE.fullmatch(value.strip())
    if not match:
        raise GoogleApiError("Google Ads customer resource is invalid")
    return match.group(1)


def _bound(value, label, maximum):
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= maximum
    ):
        raise GoogleApiLimitError("Google Ads discovery %s limit is invalid" % label)
    return value


def _nonnegative_int(value, label, maximum):
    if isinstance(value, bool):
        raise GoogleApiError("Google Ads %s is invalid" % label)
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise GoogleApiError("Google Ads %s is invalid" % label) from None
    if result < 0 or result > maximum:
        raise GoogleApiError("Google Ads %s is invalid" % label)
    return result


def _boolean(value, label):
    if not isinstance(value, bool):
        raise GoogleApiError("Google Ads %s is invalid" % label)
    return value


def _text(value, label, limit, required=True):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise GoogleApiError("Google Ads %s is invalid" % label)
    value = value.strip()
    if (
        (required and not value)
        or len(value) > limit
        or any(ord(character) < 32 for character in value)
    ):
        raise GoogleApiError("Google Ads %s is invalid" % label)
    return value

import datetime
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.marketing_center_base.services.serialization import (
    MarketingSerializationFailure,
)
from odoo.addons.meta_api_base.services.errors import MetaApiError
from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.lead_forms import (
    MetaLeadForm,
    MetaLeadFormsReadAdapter,
    fetch_meta_lead_forms,
)
from .common import create_meta_profile

_ADAPTER = (
    "odoo.addons.marketing_center_meta.models.lead_discovery.MetaLeadFormsReadAdapter"
)
_REQUEST = "odoo.addons.marketing_center_meta.services.lead_forms.graph_request"


class TestMetaLeadFormDiscoveryAdapter(SavepointCase):
    def test_page_runtime_token_is_used_instead_of_system_user_token(self):
        page = Mock()
        page.external_page_id = "123"
        page.with_context.return_value = page
        runtime = SimpleNamespace(graph_version="v26.0")
        page._resolve_graph_runtime.return_value = (runtime, "synthetic-page-token", 3)
        adapter = MetaLeadFormsReadAdapter(
            page, expected_page_revision=3, expected_app_revision=5
        )
        with patch(_REQUEST, return_value={"data": []}) as request:
            adapter.discover_lead_forms("123")
        self.assertEqual(request.call_args.args[1], "synthetic-page-token")
        page._resolve_graph_runtime.assert_called_once_with(
            expected_page_revision=3, expected_app_revision=5
        )
        with self.assertRaises(MetaApiError), patch(_REQUEST) as request:
            adapter.discover_lead_forms("456")
        request.assert_not_called()

    def _form(self, **changes):
        row = {
            "id": "300000000000001",
            "name": "Formulário principal",
            "status": "ACTIVE",
            "leads_count": 10,
            "created_time": "2026-09-01T10:00:00+0000",
        }
        row.update(changes)
        return row

    def _fetch(self):
        return fetch_meta_lead_forms(
            SimpleNamespace(graph_version="v26.0"), "synthetic-token", "100000000000101"
        )

    def test_discovery_reads_only_form_metadata_and_uses_cursor_not_next_url(self):
        first = {
            "data": [self._form()],
            "paging": {
                "next": "https://untrusted.example/path?access_token=must-never-follow",
                "cursors": {"after": "cursor-2"},
            },
        }
        second = {
            "data": [self._form(id="300000000000002", status="ARCHIVED")],
            "paging": {},
        }
        with patch(_REQUEST, side_effect=[first, second]) as request:
            forms = self._fetch()
        self.assertEqual([form.status for form in forms], ["ACTIVE", "ARCHIVED"])
        self.assertEqual(forms[0].created_at, datetime.datetime(2026, 9, 1, 10))
        for call in request.call_args_list:
            self.assertEqual(call.args[2:4], ("GET", "100000000000101/leadgen_forms"))
            self.assertEqual(
                call.kwargs["params"]["fields"],
                "id,name,status,leads_count,created_time",
            )
            self.assertNotIn("access_token", call.kwargs["params"])
        self.assertEqual(
            request.call_args_list[1].kwargs["params"]["after"], "cursor-2"
        )
        self.assertNotIn("synthetic-token", str(forms))

    def test_cycle_and_page_ceiling_reject_incomplete_inventory(self):
        def page(cursor):
            return {
                "data": [],
                "paging": {"next": "private-url", "cursors": {"after": cursor}},
            }

        with patch(
            _REQUEST, side_effect=[page("A"), page("B"), page("A")]
        ), self.assertRaises(MetaApiError):
            self._fetch()
        with patch(
            "odoo.addons.marketing_center_meta.services.lead_forms._MAX_PAGES", 2
        ), patch(_REQUEST, side_effect=[page("A"), page("B")]), self.assertRaises(
            MetaApiError
        ):
            self._fetch()

    def test_malformed_provider_rows_and_pagination_fail_closed(self):
        invalid = [
            {"data": None},
            {"data": [self._form(id="invalid/path")]},
            {"data": [self._form(name="")]},
            {"data": [self._form(status="not a status")]},
            {"data": [self._form(leads_count=True)]},
            {"data": [self._form(leads_count=-1)]},
            {"data": [self._form(created_time="not-a-date")]},
            {"data": [self._form()], "paging": ["not-an-object"]},
            {"data": [self._form()], "paging": {"next": "url", "cursors": {}}},
            {"data": [self._form()] * 101},
            {"data": [self._form(), self._form(name="Conflicting form")]},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), patch(
                _REQUEST, return_value=payload
            ), self.assertRaises(MetaApiError):
                self._fetch()

    def test_equal_repeated_form_converges_but_unknown_status_is_never_active(self):
        with patch(
            _REQUEST,
            return_value={
                "data": [self._form(status="PENDING"), self._form(status="PENDING")]
            },
        ):
            forms = self._fetch()
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0].status, "PENDING")

    def test_unsupported_graph_version_and_invalid_page_never_call_transport(self):
        for app, page in [
            (SimpleNamespace(graph_version="v25.0"), "123"),
            (SimpleNamespace(graph_version="v26.0"), "../me"),
        ]:
            with patch(_REQUEST) as request, self.assertRaises(MetaApiError):
                fetch_meta_lead_forms(app, "synthetic-token", page)
            request.assert_not_called()


class TestMetaLeadFormDiscovery(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app, cls.profile = create_meta_profile(
            cls.env,
            name="Form discovery",
            external_app_id="100000000000921",
            app_secret_ref="ODOO_META_DISCOVERY_APP",
            access_token_ref="ODOO_META_DISCOVERY_LEADS",
            reader_kind="lead_reader",
        )
        cls.ads_profile = cls.env["marketing.center.meta.profile"].create(
            {
                "name": "Discovery Ads scope",
                "company_id": cls.env.company.id,
                "meta_app_id": cls.app.id,
                "reader_kind": "ads_reader",
                "credential_backend": "environment",
                "access_token_ref": "ODOO_META_DISCOVERY_ADS",
            }
        )
        cls.endpoint = cls.env["meta.webhook.endpoint"].create(
            {
                "name": "Existing discovery endpoint",
                "app_id": cls.app.id,
                "credential_backend": "environment",
                "verify_token_ref": "ODOO_META_DISCOVERY_VERIFY",
            }
        )
        cls.page = cls.env["meta.webhook.page"].create(
            {
                "name": "Existing discovery Page",
                "endpoint_id": cls.endpoint.id,
                "external_page_id": "100000000000101",
                "credential_backend": "environment",
                "access_token_ref": "ODOO_META_DISCOVERY_PAGE",
            }
        )
        cls.source = cls.env["marketing.center.source"].create(
            {
                "name": "Discovery source",
                "company_id": cls.env.company.id,
                "service": "meta.ads",
                "external_account_ref": "act_%s" % uuid.uuid4().int,
                "state": "active",
            }
        )
        cls.connection = cls.env["marketing.center.connection"].create(
            {
                "name": "Existing discovery Ads reader",
                "source_id": cls.source.id,
                "adapter_key": "meta.graph",
                "purpose": "reader",
                "meta_profile_id": cls.ads_profile.id,
            }
        )
        cls.Wizard = cls.env["marketing.center.meta.lead.discovery"]
        cls.Route = cls.env["marketing.center.meta.lead.route"]

    def _values(self, **changes):
        values = {
            "company_id": self.env.company.id,
            "webhook_page_id": self.page.id,
            "lead_profile_id": self.profile.id,
            "source_id": self.source.id,
            "initial_sync_period": "new",
        }
        values.update(changes)
        return values

    def _forms(self):
        return (
            MetaLeadForm("300000000000001", "Formulário ativo", "ACTIVE", 10, None),
            MetaLeadForm(
                "300000000000002", "Formulário arquivado", "ARCHIVED", 4, None
            ),
        )

    def _discover(self, wizard=None, forms=None):
        wizard = wizard or self.Wizard.create(self._values())
        with patch(_ADAPTER) as adapter:
            adapter.return_value.discover_lead_forms.return_value = (
                forms or self._forms()
            )
            wizard.action_discover()
        return wizard

    def _existing(self, **changes):
        values = self._values()
        values.pop("initial_sync_period")
        values.update(name="Existing route", external_form_id="300000000000001")
        values.update(changes)
        return self.Route.create(values)

    def _user(self, group):
        return (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Discovery user",
                    "login": "discovery-%s" % uuid.uuid4(),
                    "company_id": self.env.company.id,
                    "company_ids": [Command.set(self.env.company.ids)],
                    "groups_id": [Command.set(self.env.ref(group).ids)],
                }
            )
        )

    def test_discovery_is_preview_only_and_keeps_archived_forms_visible(self):
        before = self.Route.with_context(active_test=False).search_count([])
        endpoints = self.env["meta.webhook.endpoint"].search_count([])
        subscriptions = self.env["meta.webhook.subscription"].search_count([])
        with trap_jobs() as jobs:
            wizard = self._discover()
            jobs.assert_jobs_count(0)
        self.assertEqual(wizard.state, "preview")
        self.assertEqual(len(wizard.line_ids), 2)
        self.assertFalse(any(wizard.line_ids.mapped("selected")))
        self.assertEqual(
            set(wizard.line_ids.mapped("disposition")), {"new", "archived"}
        )
        self.assertEqual(
            self.Route.with_context(active_test=False).search_count([]), before
        )
        self.assertEqual(self.env["meta.webhook.endpoint"].search_count([]), endpoints)
        self.assertEqual(
            self.env["meta.webhook.subscription"].search_count([]), subscriptions
        )

    def test_selected_active_form_creates_one_route_without_automatic_crm(self):
        wizard = self._discover(
            self.Wizard.with_context(default_crm_auto_create_lead=True).create(
                self._values()
            )
        )
        line = wizard.line_ids.filtered(lambda row: row.form_status == "ACTIVE")
        # Match the x2many update sent by the editable Odoo tree, not just line.write.
        wizard.write({"line_ids": [Command.update(line.id, {"selected": True})]})
        with patch(_ADAPTER) as adapter, trap_jobs():
            adapter.return_value.discover_lead_forms.return_value = self._forms()
            wizard.action_configure()
            wizard.action_configure()
        routes = self.Route.search([("webhook_page_id", "=", self.page.id)])
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes.external_form_id, line.external_form_id)
        self.assertEqual(routes.webhook_page_id.endpoint_id, self.endpoint)
        self.assertEqual(routes.initial_sync_period, "new")
        self.assertTrue(routes.reconcile_enabled)
        if "crm_auto_create_lead" in routes._fields:
            self.assertFalse(routes.crm_auto_create_lead)
        self.assertFalse(
            self.Route.search([("external_form_id", "=", "300000000000002")])
        )

    def test_historical_selection_is_explicit_and_enqueues_only_new_route(self):
        wizard = self._discover(
            self.Wizard.create(self._values(initial_sync_period="30"))
        )
        wizard.line_ids.filtered(lambda row: row.form_status == "ACTIVE").write(
            {"selected": True}
        )
        with patch(_ADAPTER) as adapter, trap_jobs():
            adapter.return_value.discover_lead_forms.return_value = self._forms()
            wizard.action_configure()
        route = wizard.line_ids.filtered("selected").route_id
        self.assertEqual(route.initial_sync_period, "30")
        self.assertTrue(route.reconcile_job_uuid)

    def test_existing_archived_route_and_crm_policy_are_never_rewritten(self):
        values = {
            "active": False,
            "reconcile_enabled": False,
            "reconcile_lookback_hours": 1,
        }
        if "crm_auto_create_lead" in self.Route._fields:
            values["crm_auto_create_lead"] = True
        route = self._existing(**values)
        before = route.read(
            list(values) + ["name", "lead_profile_id", "source_id", "route_revision"]
        )[0]
        wizard = self._discover()
        line = wizard.line_ids.filtered(
            lambda row: row.external_form_id == route.external_form_id
        )
        self.assertEqual(line.disposition, "configured")
        self.assertEqual(line.route_id, route)
        line.write({"selected": True})
        with patch(_ADAPTER) as adapter, trap_jobs() as jobs:
            adapter.return_value.discover_lead_forms.return_value = self._forms()
            wizard.action_configure()
            jobs.assert_jobs_count(0)
        self.assertEqual(
            route.read(
                list(values)
                + ["name", "lead_profile_id", "source_id", "route_revision"]
            )[0],
            before,
        )

    def test_archived_or_removed_after_preview_cannot_create_route(self):
        wizard = self._discover()
        active = wizard.line_ids.filtered(lambda row: row.form_status == "ACTIVE")
        active.write({"selected": True})
        changed = (
            MetaLeadForm(active.external_form_id, active.name, "ARCHIVED", 10, None),
        )
        with patch(_ADAPTER) as adapter, self.assertRaises(UserError):
            adapter.return_value.discover_lead_forms.return_value = changed
            wizard.action_configure()
        active.write({"selected": False})
        wizard.line_ids.filtered(lambda row: row.form_status == "ARCHIVED").write(
            {"selected": True}
        )
        with patch(_ADAPTER) as adapter, self.assertRaises(UserError):
            adapter.return_value.discover_lead_forms.return_value = self._forms()
            wizard.action_configure()
        self.assertFalse(self.Route.search([("webhook_page_id", "=", self.page.id)]))

    def test_provider_failure_does_not_expose_error_details_or_create_routes(self):
        wizard = self.Wizard.create(self._values())
        with patch(_ADAPTER) as adapter, self.assertRaises(UserError) as failure:
            adapter.return_value.discover_lead_forms.side_effect = MetaApiError(
                "secret-token@example.invalid"
            )
            wizard.action_discover()
        self.assertNotIn("secret-token", str(failure.exception))
        self.assertEqual(wizard.state, "draft")
        self.assertFalse(wizard.line_ids)

    def test_viewer_cannot_open_create_or_discover(self):
        viewer = self._user("marketing_center_base.group_marketing_center_viewer")
        with self.assertRaises(AccessError):
            self.Wizard.with_user(viewer).create(self._values())
        route = self._existing()
        with self.assertRaises(AccessError):
            route.with_user(viewer).action_open_discovery()
        wizard = self.Wizard.create(self._values())
        with self.assertRaises(AccessError):
            wizard.with_user(viewer).action_discover()

    def test_marketing_admin_without_settings_can_discover_with_managed_profile(self):
        admin = self._user("marketing_center_base.group_marketing_center_admin")
        self.assertFalse(admin.has_group("base.group_system"))
        wizard = self.Wizard.with_user(admin).create(self._values())
        with patch(_ADAPTER) as adapter:
            adapter.return_value.discover_lead_forms.return_value = self._forms()
            wizard.action_discover()
            self.assertTrue(adapter.call_args.args[0].env.su)
        self.assertEqual(len(wizard.line_ids), 2)
        route = self._existing()
        action = route.with_user(admin).action_open_discovery()
        self.assertEqual(action["res_model"], wizard._name)
        self.assertEqual(action["context"]["default_source_id"], self.source.id)

    def test_company_page_profile_and_source_app_scope_are_enforced(self):
        other_company = self.env["res.company"].create(
            {"name": "Other discovery company"}
        )
        other_app, other_profile = create_meta_profile(
            self.env,
            name="Other App",
            external_app_id="100000000000922",
            app_secret_ref="ODOO_META_OTHER_APP",
            access_token_ref="ODOO_META_OTHER_LEADS",
            reader_kind="lead_reader",
        )
        self.assertNotEqual(other_app, self.app)
        for changes, expected_error in (
            ({"company_id": other_company.id}, UserError),
            ({"lead_profile_id": other_profile.id}, ValidationError),
            ({"lead_profile_id": self.ads_profile.id}, ValidationError),
        ):
            with self.env.cr.savepoint(), self.assertRaises(expected_error):
                self.Wizard.create(self._values(**changes))
        other_source = self.source.copy(
            {"external_account_ref": "act_%s" % uuid.uuid4().int}
        )
        with self.env.cr.savepoint(), self.assertRaises(ValidationError):
            self.Wizard.create(self._values(source_id=other_source.id))

    def test_rpc_cannot_forge_provider_results_or_reassign_line_to_another_wizard(self):
        wizard = self._discover()
        line = wizard.line_ids[0]
        with self.assertRaises(AccessError):
            wizard.write({"state": "done"})
        with self.assertRaises(AccessError):
            self.Wizard.with_context(default_state="preview").create(self._values())
        with self.assertRaises(AccessError):
            self.Wizard.with_context(default_line_ids=[]).create(self._values())
        with self.assertRaises(AccessError):
            line.write({"external_form_id": "666"})
        with self.assertRaises(AccessError):
            wizard.write({"line_ids": [Command.create({"name": "Forged"})]})
        other = self._discover()
        with self.assertRaises(AccessError):
            wizard.write(
                {"line_ids": [Command.update(other.line_ids[0].id, {"selected": True})]}
            )
        wizard.write({"initial_sync_period": "7"})
        self.assertEqual(wizard.state, "preview")
        self.assertEqual(len(wizard.line_ids), 2)
        wizard.write({"webhook_page_id": self.page.id})
        self.assertFalse(wizard.line_ids)
        self.assertEqual(wizard.state, "draft")

    def test_profile_revision_change_invalidates_confirmation(self):
        wizard = self._discover()
        wizard.line_ids.filtered(lambda row: row.form_status == "ACTIVE").write(
            {"selected": True}
        )
        self.profile.write({"access_token_ref": "ODOO_META_DISCOVERY_ROTATED"})
        with self.assertRaises(UserError), patch(_ADAPTER) as adapter:
            wizard.action_configure()
        adapter.assert_not_called()

    def test_concurrent_configuration_requires_fresh_transaction_without_duplicate_create(
        self,
    ):
        wizard = self._discover()
        wizard.line_ids.filtered(lambda row: row.form_status == "ACTIVE").write(
            {"selected": True}
        )
        with patch(_ADAPTER) as adapter, patch(
            "odoo.addons.marketing_center_meta.models.lead_discovery."
            "acquire_advisory_xact_lock",
            side_effect=MarketingSerializationFailure(
                "Synthetic competing configuration"
            ),
        ), self.assertRaises(MarketingSerializationFailure):
            adapter.return_value.discover_lead_forms.return_value = self._forms()
            wizard.action_configure()
        self.assertFalse(self.Route.search([("webhook_page_id", "=", self.page.id)]))

    def test_route_action_prefills_the_existing_page_profile_and_source(self):
        route = self._existing()
        action = route.action_open_discovery()
        self.assertEqual(action["res_model"], self.Wizard._name)
        self.assertEqual(action["context"]["default_webhook_page_id"], self.page.id)
        self.assertEqual(action["context"]["default_lead_profile_id"], self.profile.id)
        self.assertEqual(action["context"]["default_source_id"], self.source.id)
        self.assertEqual(action["context"]["default_initial_sync_period"], "new")

    def test_defaults_fill_only_a_unique_active_combination_and_preserve_context(self):
        # Upgrade tests can run beside seeded operational routes. An isolated
        # company makes absence/uniqueness assertions independent of that data.
        company = self.env["res.company"].create({"name": "Isolated form defaults"})
        wizard_model = self.Wizard.with_company(company)
        local_env = wizard_model.env
        app, profile = create_meta_profile(
            local_env,
            name="Isolated defaults",
            external_app_id=str(uuid.uuid4().int),
            app_secret_ref="ODOO_META_DEFAULTS_APP",
            access_token_ref="ODOO_META_DEFAULTS_LEADS",
            reader_kind="lead_reader",
        )
        endpoint = local_env["meta.webhook.endpoint"].create(
            {
                "name": "Isolated defaults endpoint",
                "app_id": app.id,
                "credential_backend": "environment",
                "verify_token_ref": "ODOO_META_DEFAULTS_VERIFY",
            }
        )
        page = local_env["meta.webhook.page"].create(
            {
                "name": "Isolated defaults Page",
                "endpoint_id": endpoint.id,
                "external_page_id": str(uuid.uuid4().int),
                "credential_backend": "environment",
                "access_token_ref": "ODOO_META_DEFAULTS_PAGE",
            }
        )
        source = local_env["marketing.center.source"].create(
            {
                "name": "Isolated defaults source",
                "service": "meta.ads",
                "external_account_ref": "act_%s" % uuid.uuid4().int,
                "state": "active",
            }
        )
        route_values = {
            "name": "Isolated defaults route",
            "company_id": company.id,
            "webhook_page_id": page.id,
            "lead_profile_id": profile.id,
            "source_id": source.id,
        }
        route_model = local_env["marketing.center.meta.lead.route"]
        fields_list = ["company_id", "webhook_page_id", "lead_profile_id", "source_id"]
        empty = wizard_model.default_get(fields_list)
        self.assertFalse(empty.get("webhook_page_id"))
        route_model.create(dict(route_values, external_form_id="300000000000001"))
        route_model.create(dict(route_values, external_form_id="300000000000003"))
        values = wizard_model.default_get(fields_list)
        self.assertEqual(values["webhook_page_id"], page.id)
        self.assertEqual(values["lead_profile_id"], profile.id)
        self.assertEqual(values["source_id"], source.id)
        explicit = wizard_model.with_context(default_source_id=False).default_get(
            fields_list
        )
        self.assertFalse(explicit.get("source_id"))
        other_profile = local_env["marketing.center.meta.profile"].create(
            {
                "name": "Another lead reader",
                "meta_app_id": app.id,
                "reader_kind": "lead_reader",
                "credential_backend": "environment",
                "access_token_ref": "ODOO_META_DISCOVERY_ANOTHER_LEADS",
            }
        )
        route_model.create(
            dict(
                route_values,
                external_form_id="300000000000004",
                lead_profile_id=other_profile.id,
            )
        )
        ambiguous = wizard_model.default_get(fields_list)
        self.assertFalse(ambiguous.get("webhook_page_id"))
        self.assertFalse(ambiguous.get("lead_profile_id"))
        self.assertFalse(ambiguous.get("source_id"))

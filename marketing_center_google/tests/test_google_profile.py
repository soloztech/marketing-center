import uuid
from unittest.mock import patch

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.google_api_base.services.errors import GoogleApiTransientError
from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.job import Job

from ..services.adapter import GoogleDiscoveredCustomer, GoogleDiscoveryResult
from ..services.tokens import MARKETING_GOOGLE_PROFILE_RUNTIME_TOKEN
from .common import create_google_profile, project_google_source


class TestGoogleProfileAndProjection(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.identity, cls.profile = create_google_profile(cls.env)

    def _running_profile_job(self, method_name, *, completed_tries):
        kwargs = {
            "expected_profile_revision": self.profile.profile_revision,
            "expected_identity_revision": self.profile.identity_revision,
        }
        job = Job(
            getattr(self.profile, method_name),
            kwargs=kwargs,
            max_retries=8,
        )
        job.store()
        job.retry = completed_tries
        job.set_started()
        job.store()
        return job

    def test_real_queue_job_terminal_validation_projects_degraded_health(self):
        job = self._running_profile_job(
            "_job_validate_google_profile",
            completed_tries=7,
        )
        adapter_path = (
            "odoo.addons.marketing_center_google.models.google_service."
            "GoogleMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class:
            adapter_class.return_value.validate.side_effect = GoogleApiTransientError(
                "synthetic transient validation",
                request_id="terminal-validation-request",
            )
            result = job.perform()

        self.profile.invalidate_recordset(
            [
                "health_state",
                "verified_at",
                "last_error_class",
                "last_error_message",
                "last_request_id",
            ]
        )
        self.assertEqual(result, {"failed": True, "retry_exhausted": True})
        self.assertEqual(job.retry, 8)
        self.assertEqual(self.profile.health_state, "degraded")
        self.assertTrue(self.profile.verified_at)
        self.assertEqual(self.profile.last_error_class, "transient")
        self.assertIn("retry budget", self.profile.last_error_message)
        self.assertEqual(
            self.profile.last_request_id,
            "terminal-validation-request",
        )

    def test_real_queue_job_nonterminal_validation_still_retries(self):
        job = self._running_profile_job(
            "_job_validate_google_profile",
            completed_tries=6,
        )
        adapter_path = (
            "odoo.addons.marketing_center_google.models.google_service."
            "GoogleMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class:
            adapter_class.return_value.validate.side_effect = GoogleApiTransientError(
                "synthetic transient validation"
            )
            with self.assertRaises(RetryableJobError):
                job.perform()

        self.profile.invalidate_recordset(["health_state", "verified_at"])
        self.assertEqual(job.retry, 7)
        self.assertEqual(self.profile.health_state, "unknown")
        self.assertFalse(self.profile.verified_at)

    def test_real_queue_job_terminal_discovery_projects_failure(self):
        job = self._running_profile_job(
            "_job_discover_google_sources",
            completed_tries=7,
        )
        adapter_path = (
            "odoo.addons.marketing_center_google.models.google_service."
            "GoogleMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class:
            (
                adapter_class.return_value.discover_customers.side_effect
            ) = GoogleApiTransientError("synthetic transient discovery")
            result = job.perform()

        self.profile.invalidate_recordset(["health_state", "last_error_class"])
        self.assertEqual(
            result,
            {
                "discovered": 0,
                "failed": True,
                "retry_exhausted": True,
            },
        )
        self.assertEqual(job.retry, 8)
        self.assertEqual(self.profile.health_state, "degraded")
        self.assertEqual(self.profile.last_error_class, "transient")

    def test_identity_and_profile_a_b_a_rotation_is_monotonic(self):
        _customer, _source, connection = project_google_source(self.env, self.profile)
        original_ref = self.identity.developer_token_ref
        identity_a = self.identity.revision
        profile_a = self.profile.profile_revision
        self.identity.write({"developer_token_ref": "GOOGLE_TOKEN_B"})
        identity_b = self.identity.revision
        self.profile._adopt_current_identity_revision()
        profile_b = self.profile.profile_revision
        self.assertGreater(identity_b, identity_a)
        self.assertGreater(profile_b, profile_a)
        self.assertEqual(connection.state, "paused")

        self.identity.write({"developer_token_ref": original_ref})
        self.profile._adopt_current_identity_revision()
        self.assertGreater(self.identity.revision, identity_b)
        self.assertGreater(self.profile.profile_revision, profile_b)
        self.assertEqual(self.profile.identity_revision, self.identity.revision)

    def test_profile_noop_write_preserves_revision_health_and_connections(self):
        _customer, _source, connection = project_google_source(self.env, self.profile)
        initial_connection_state = connection.state
        self.profile.with_context(
            marketing_google_profile_runtime_token=(
                MARKETING_GOOGLE_PROFILE_RUNTIME_TOKEN
            )
        ).write({"health_state": "healthy"})
        revision = self.profile.profile_revision

        self.profile.write(
            {
                "google_identity_id": self.profile.google_identity_id.id,
                "max_discovery_depth": self.profile.max_discovery_depth,
            }
        )
        self.profile.invalidate_recordset(["profile_revision", "health_state"])
        connection.invalidate_recordset(["state"])

        self.assertEqual(self.profile.profile_revision, revision)
        self.assertEqual(self.profile.health_state, "healthy")
        self.assertEqual(connection.state, initial_connection_state)

    def test_identical_discovery_does_not_rotate_source_or_binding(self):
        customer, source, connection = project_google_source(self.env, self.profile)
        source_revision = source.configuration_revision
        binding_revision = connection.binding_revision
        self.env["marketing.center.google.service"]._project_customer(
            self.profile, customer
        )
        self.assertEqual(source.configuration_revision, source_revision)
        self.assertEqual(connection.binding_revision, binding_revision)
        self.assertTrue(source.last_observed_at)

    def test_effective_login_customer_is_protected_and_revision_fenced(self):
        customer, _source, connection = project_google_source(
            self.env,
            self.profile,
            login_customer_id="1111111111",
        )
        self.assertEqual(connection.google_login_customer_id, "1111111111")
        initial_revision = connection.binding_revision
        with self.assertRaises(AccessError):
            connection.write({"google_login_customer_id": "2222222222"})

        moved = GoogleDiscoveredCustomer(
            **{
                **customer.__dict__,
                "access_login_customer_id": "2222222222",
            }
        )
        self.env["marketing.center.google.service"]._project_customer(
            self.profile,
            moved,
        )
        connection.invalidate_recordset(
            ["binding_revision", "google_login_customer_id"]
        )
        self.assertEqual(connection.google_login_customer_id, "2222222222")
        self.assertGreater(connection.binding_revision, initial_revision)

    def test_fixed_identity_login_must_match_projected_binding(self):
        self.identity.write({"login_customer_id": "3333333333"})
        self.profile._adopt_current_identity_revision()
        with self.assertRaises(ValidationError):
            project_google_source(self.env, self.profile)
        _customer, _source, connection = project_google_source(
            self.env,
            self.profile,
            customer_id="4444444444",
            login_customer_id="3333333333",
        )
        self.assertEqual(connection.google_login_customer_id, "3333333333")

    def test_archived_source_is_never_reactivated_by_discovery(self):
        customer, source, connection = project_google_source(self.env, self.profile)
        source.write({"active": False})
        connection.write({"active": False})
        self.env["marketing.center.google.service"]._project_customer(
            self.profile, customer
        )
        self.assertFalse(source.active)
        self.assertFalse(connection.active)

    def test_same_customer_projects_to_distinct_company_scopes(self):
        customer, source, _connection = project_google_source(self.env, self.profile)
        other_company = self.env["res.company"].create(
            {"name": "Google other company %s" % uuid.uuid4()}
        )
        _other_identity, other_profile = create_google_profile(
            self.env,
            name="Other Google laboratory %s" % uuid.uuid4(),
            company=other_company,
        )
        other_source = (
            self.env["marketing.center.google.service"]
            .with_context(allowed_company_ids=[self.env.company.id, other_company.id])
            ._project_customer(other_profile, customer)
        )
        self.assertNotEqual(source, other_source)
        self.assertEqual(source.company_id, self.env.company)
        self.assertEqual(other_source.company_id, other_company)

    def test_profile_identity_and_runtime_fields_are_protected(self):
        with self.assertRaises(AccessError):
            self.profile.write({"identity_revision": self.identity.revision + 1})
        with self.assertRaises(AccessError):
            self.profile.write({"health_state": "healthy"})
        other_company = self.env["res.company"].create(
            {"name": "Google invalid company %s" % uuid.uuid4()}
        )
        other_identity, other_profile = create_google_profile(
            self.env,
            name="Google invalid identity %s" % uuid.uuid4(),
            company=other_company,
        )
        other_profile.write({"active": False})
        with self.assertRaises(ValidationError):
            self.env["marketing.center.google.profile"].create(
                {
                    "name": "Cross-company Google profile",
                    "company_id": self.env.company.id,
                    "google_identity_id": other_identity.id,
                }
            )

    def test_ready_connection_cannot_be_rebound_manually(self):
        _customer, _source, connection = project_google_source(self.env, self.profile)
        _other_identity, other_profile = create_google_profile(
            self.env,
            name="Google manual rebind %s" % uuid.uuid4(),
        )
        with self.assertRaises(AccessError):
            connection.write({"google_profile_id": other_profile.id})

    def test_google_source_external_id_is_coherent_and_immutable(self):
        _customer, source, _connection = project_google_source(
            self.env,
            self.profile,
        )
        with self.assertRaises(AccessError):
            source.write({"external_account_id": "9999999999"})
        source_without_evidence = self.env["marketing.center.source"].create(
            {
                "name": "Invalid Google source",
                "company_id": self.env.company.id,
                "service": "meta.ads",
                "external_account_ref": "account/example",
                "external_account_id": "example",
                "currency_id": self.env.company.currency_id.id,
                "timezone": "UTC",
            }
        )
        with self.assertRaises(ValidationError):
            source_without_evidence.write(
                {
                    "service": "google.ads",
                    "external_account_ref": "customers/1234567890",
                    "external_account_id": "9999999999",
                }
            )

    def test_disabled_customer_degrades_without_deleting_evidence(self):
        customer, source, connection = project_google_source(self.env, self.profile)
        unavailable = GoogleDiscoveredCustomer(
            **{
                **customer.__dict__,
                "status": "suspended",
            }
        )
        self.env["marketing.center.google.service"]._project_customer(
            self.profile, unavailable
        )
        self.assertEqual(source.state, "attention")
        self.assertEqual(connection.state, "degraded")
        self.assertTrue(source.active)
        self.assertTrue(connection.active)

    def test_discovery_success_preserves_a_newer_connection_cooldown(self):
        customer, _source, connection = project_google_source(self.env, self.profile)
        sync_service = self.env["marketing.center.google.sync.service"]
        sync_service._mark_connection_cooldown(
            connection,
            3600,
            classification="transient",
        )
        connection.invalidate_recordset(
            ["cooldown_until", "health_state", "last_health_error_class"]
        )
        deadline = connection.cooldown_until

        self.env["marketing.center.google.service"]._project_customer(
            self.profile,
            customer,
        )
        connection.invalidate_recordset(
            ["cooldown_until", "health_state", "last_health_error_class"]
        )
        self.assertEqual(connection.cooldown_until, deadline)
        self.assertEqual(connection.health_state, "degraded")
        self.assertEqual(connection.last_health_error_class, "transient")

    def test_discovery_projects_leaf_customers_and_skips_managers(self):
        manager = GoogleDiscoveredCustomer(
            resource_name="customers/1111111111",
            customer_id="1111111111",
            name="Manager",
            currency=self.env.company.currency_id.name,
            timezone="UTC",
            status="enabled",
            manager=True,
            test_account=False,
            hidden=False,
            depth=0,
        )
        leaf = GoogleDiscoveredCustomer(
            resource_name="customers/2222222222",
            customer_id="2222222222",
            name="Leaf",
            currency=self.env.company.currency_id.name,
            timezone="UTC",
            status="enabled",
            manager=False,
            test_account=False,
            hidden=False,
            depth=1,
        )
        result = GoogleDiscoveryResult(
            customers=(manager, leaf), root_count=1, request_count=2
        )
        adapter_path = (
            "odoo.addons.marketing_center_google.models.google_service."
            "GoogleMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class:
            adapter_class.return_value.discover_customers.return_value = result
            disposition = self.env["marketing.center.google.service"]._discover_sources(
                self.profile,
                expected_profile_revision=self.profile.profile_revision,
                expected_identity_revision=self.profile.identity_revision,
            )
        self.assertEqual(disposition["discovered"], 1)
        self.assertEqual(disposition["managers"], 1)
        sources = self.env["marketing.center.source"].search(
            [("service", "=", "google.ads")]
        )
        self.assertEqual(sources.mapped("external_account_ref"), [leaf.resource_name])

    def test_discovery_provider_io_precedes_profile_row_locks(self):
        leaf = GoogleDiscoveredCustomer(
            resource_name="customers/3333333333",
            customer_id="3333333333",
            name="Lock-free discovery leaf",
            currency=self.env.company.currency_id.name,
            timezone="UTC",
            status="enabled",
            manager=False,
            test_account=False,
            hidden=False,
            depth=1,
        )
        result = GoogleDiscoveryResult(customers=(leaf,), root_count=1, request_count=2)
        service = self.env["marketing.center.google.service"]
        service_class = type(service)
        original_lock = service_class._lock_current_profile
        lock_calls = []

        def traced_lock(recordset, *args, **kwargs):
            lock_calls.append(True)
            return original_lock(recordset, *args, **kwargs)

        def provider_discovery(*_args, **_kwargs):
            self.assertFalse(lock_calls)
            return result

        adapter_path = (
            "odoo.addons.marketing_center_google.models.google_service."
            "GoogleMarketingReadAdapter"
        )
        with patch.object(service_class, "_lock_current_profile", traced_lock):
            with patch(adapter_path) as adapter_class:
                adapter_class.return_value.discover_customers.side_effect = (
                    provider_discovery
                )
                disposition = service._discover_sources(
                    self.profile,
                    expected_profile_revision=self.profile.profile_revision,
                    expected_identity_revision=self.profile.identity_revision,
                )
        self.assertEqual(disposition["discovered"], 1)
        self.assertTrue(lock_calls)

    def test_validation_provider_io_precedes_profile_row_locks(self):
        service = self.env["marketing.center.google.service"]
        service_class = type(service)
        original_lock = service_class._lock_current_profile
        lock_calls = []

        def traced_lock(recordset, *args, **kwargs):
            lock_calls.append(True)
            return original_lock(recordset, *args, **kwargs)

        def provider_validation():
            self.assertFalse(lock_calls)
            return {"accessible_root_count": 1, "request_id": "request-safe"}

        adapter_path = (
            "odoo.addons.marketing_center_google.models.google_service."
            "GoogleMarketingReadAdapter"
        )
        with patch.object(service_class, "_lock_current_profile", traced_lock):
            with patch(adapter_path) as adapter_class:
                adapter_class.return_value.validate.side_effect = provider_validation
                disposition = service._validate_profile(
                    self.profile,
                    expected_profile_revision=self.profile.profile_revision,
                    expected_identity_revision=self.profile.identity_revision,
                )
        self.assertEqual(disposition, {"validated": True, "root_count": 1})
        self.assertTrue(lock_calls)

    def test_consumer_profile_never_persists_secret_or_raw_payload_fields(self):
        forbidden = {
            "access_token",
            "client_secret",
            "developer_token",
            "payload",
            "raw_payload",
            "refresh_token",
            "service_account_json",
        }
        self.assertFalse(forbidden.intersection(self.profile._fields))

    def test_profile_accepts_service_account_identity_without_auth_assumptions(self):
        identity = (
            self.env["google.api.identity"]
            .sudo()
            .create(
                {
                    "name": "Google service account identity %s" % uuid.uuid4(),
                    "company_id": self.env.company.id,
                    "api_version": "v25",
                    "auth_mode": "service_account",
                    "credential_backend": "environment",
                    "service_account_json_ref": "GOOGLE_ADS_READER_JSON",
                    "developer_token_ref": "GOOGLE_ADS_DEVELOPER_TOKEN",
                }
            )
        )
        profile = (
            self.env["marketing.center.google.profile"]
            .sudo()
            .create(
                {
                    "name": "Google service account profile %s" % uuid.uuid4(),
                    "company_id": self.env.company.id,
                    "google_identity_id": identity.id,
                }
            )
        )
        self.assertEqual(profile.google_identity_id, identity)
        self.assertEqual(profile.identity_revision, identity.revision)

    def test_company_record_rule_hides_foreign_profile_from_marketing_admin(self):
        other_company = self.env["res.company"].create(
            {"name": "Google ACL company %s" % uuid.uuid4()}
        )
        _identity, foreign = create_google_profile(
            self.env,
            name="Google foreign profile %s" % uuid.uuid4(),
            company=other_company,
        )
        group = self.env.ref("marketing_center_base.group_marketing_center_admin")
        user = self.env["res.users"].create(
            {
                "name": "Google marketing admin %s" % uuid.uuid4(),
                "login": "google-admin-%s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "company_ids": [(6, 0, [self.env.company.id])],
                "groups_id": [(6, 0, [group.id])],
            }
        )
        visible = self.env["marketing.center.google.profile"].with_user(user).search([])
        self.assertIn(self.profile.id, visible.ids)
        self.assertNotIn(foreign.id, visible.ids)

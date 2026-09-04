import uuid
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests.common import SavepointCase

from odoo.addons.meta_api_base.services.errors import MetaApiTransientError
from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.job import Job

from ..services.adapter import MetaAdAccount, MetaReadValidation
from ..services.tokens import MARKETING_META_CONNECTION_TOKEN
from .common import create_meta_profile


class TestMarketingCenterMetaService(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.meta_app, cls.profile = create_meta_profile(
            cls.env,
            name="Meta service laboratory",
            external_app_id="987654321",
            app_secret_ref="ODOO_META_SERVICE_APP_SECRET",
            access_token_ref="ODOO_META_SERVICE_READER_TOKEN",
        )
        cls.validation = MetaReadValidation(
            token_type="system_user",
            expires_at=0,
            data_access_expires_at=0,
            scopes=("ads_read",),
            capabilities={
                "read_entities": True,
                "read_metrics": True,
                "receive_leads": False,
            },
        )
        cls.account = MetaAdAccount(
            external_ref="act_987",
            external_id="987",
            name="Soloz Meta Laboratory",
            currency=cls.env.company.currency_id.name,
            timezone="America/Sao_Paulo",
            account_status=1,
            disable_reason=0,
        )

    def _running_profile_job(self, method_name, *, completed_tries):
        kwargs = {
            "expected_profile_revision": self.profile.profile_revision,
            "expected_app_revision": self.meta_app.revision,
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

    def test_sync_run_job_action_is_system_only_and_opens_the_exact_job(self):
        source = self.env["marketing.center.source"].create(
            {
                "name": "Meta queue action source",
                "company_id": self.env.company.id,
                "service": "meta.ads",
                "external_account_ref": "act_987654322",
                "currency_id": self.env.company.currency_id.id,
                "timezone": "UTC",
                "state": "active",
            }
        )
        connection = self.env["marketing.center.connection"].create(
            {
                "name": "Meta queue action reader",
                "source_id": source.id,
                "adapter_key": "meta.graph",
                "purpose": "reader",
                "meta_profile_id": self.profile.id,
                "state": "draft",
            }
        )
        connection.with_context(
            marketing_meta_connection_token=MARKETING_META_CONNECTION_TOKEN
        ).write({"state": "ready"})
        service = self.env["marketing.center.meta.catalog.service"]
        run = service._plan_sweep(
            source,
            connection,
            trigger_kind="manual",
            trigger_ref=str(uuid.uuid4()),
        )
        cursor_sequence = service._restart_catalog_cursor(run)
        service._enqueue_page(run, cursor_sequence)
        job = self.env["queue.job"].search([("uuid", "=", run.queue_job_uuid)], limit=1)
        self.assertTrue(job)

        viewer_group = self.env.ref(
            "marketing_center_base.group_marketing_center_viewer"
        )
        viewer = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Sync job viewer",
                    "login": "sync-job-viewer-%s" % uuid.uuid4(),
                    "email": "sync-job-viewer@example.invalid",
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [(6, 0, viewer_group.ids)],
                }
            )
        )
        with self.assertRaises(AccessError):
            run.with_user(viewer).action_open_queue_job()

        action = run.action_open_queue_job()
        self.assertEqual(action["res_model"], "queue.job")
        self.assertEqual(action["res_id"], job.id)
        self.assertEqual(action["view_mode"], "form")

    def test_real_queue_job_terminal_validation_projects_degraded_health(self):
        job = self._running_profile_job(
            "_job_validate_read_profile",
            completed_tries=7,
        )
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.meta_service."
            "MetaMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class:
            adapter_class.return_value.validate.side_effect = MetaApiTransientError(
                "synthetic transient validation"
            )
            result = job.perform()

        self.profile.invalidate_recordset(
            ["health_state", "verified_at", "last_error_class", "last_error_message"]
        )
        self.assertFalse(result)
        self.assertEqual(job.retry, 8)
        self.assertEqual(self.profile.health_state, "degraded")
        self.assertTrue(self.profile.verified_at)
        self.assertEqual(self.profile.last_error_class, "transient")
        self.assertIn("retry budget", self.profile.last_error_message)

    def test_real_queue_job_nonterminal_validation_still_retries(self):
        job = self._running_profile_job(
            "_job_validate_read_profile",
            completed_tries=6,
        )
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.meta_service."
            "MetaMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class:
            adapter_class.return_value.validate.side_effect = MetaApiTransientError(
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
            "_job_discover_read_sources",
            completed_tries=7,
        )
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.meta_service."
            "MetaMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class:
            adapter_class.return_value.validate.side_effect = MetaApiTransientError(
                "synthetic transient discovery"
            )
            result = job.perform()

        self.profile.invalidate_recordset(["health_state", "last_error_class"])
        self.assertEqual(
            result,
            {"discovered": 0, "failed": True, "retry_exhausted": True},
        )
        self.assertEqual(job.retry, 8)
        self.assertEqual(self.profile.health_state, "degraded")
        self.assertEqual(self.profile.last_error_class, "transient")

    def test_terminal_retry_cannot_poison_a_rotated_profile(self):
        job = self._running_profile_job(
            "_job_validate_read_profile",
            completed_tries=7,
        )
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.meta_service."
            "MetaMarketingReadAdapter"
        )

        def rotate_then_fail():
            self.meta_app.write(
                {"app_secret_ref": "ODOO_META_SERVICE_APP_SECRET_ROTATED_TERMINAL"}
            )
            raise MetaApiTransientError("synthetic stale terminal validation")

        with patch(adapter_path) as adapter_class:
            adapter_class.return_value.validate.side_effect = rotate_then_fail
            result = job.perform()

        self.profile.invalidate_recordset(
            ["profile_revision", "health_state", "verified_at", "last_error_class"]
        )
        self.assertEqual(result, {"stale": True})
        self.assertEqual(job.retry, 8)
        self.assertEqual(self.profile.health_state, "unknown")
        self.assertFalse(self.profile.verified_at)
        self.assertFalse(self.profile.last_error_class)

    def test_discovery_projects_neutral_source_and_connection_idempotently(self):
        service = self.env["marketing.center.meta.service"]
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.meta_service."
            "MetaMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class:
            adapter = adapter_class.return_value
            adapter.validate.return_value = self.validation
            adapter.discover_ad_accounts.return_value = (self.account,)
            first = service._discover_sources(
                self.profile,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.meta_app.revision,
            )
            source = self.env["marketing.center.source"].search(
                [
                    ("service", "=", "meta.ads"),
                    ("external_account_ref", "=", "act_987"),
                ]
            )
            connection = self.env["marketing.center.connection"].search(
                [
                    ("source_id", "=", source.id),
                    ("adapter_key", "=", "meta.graph"),
                ]
            )
            source_revision = source.configuration_revision
            binding_revision = connection.binding_revision
            second = service._discover_sources(
                self.profile,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.meta_app.revision,
            )
        self.assertEqual(first["discovered"], 1)
        self.assertEqual(second["discovered"], 1)
        self.assertEqual(len(source), 1)
        self.assertEqual(source.configuration_revision, source_revision)
        self.assertEqual(source.external_account_id, "987")
        self.assertFalse(source.write_enabled)
        self.assertEqual(len(connection), 1)
        self.assertEqual(connection.binding_revision, binding_revision)
        self.assertEqual(connection.meta_profile_id, self.profile)
        self.assertEqual(connection.state, "ready")
        self.assertEqual(connection.health_state, "healthy")
        self.assertTrue(connection.effective_capabilities_json["read_entities"])
        self.assertEqual(self.profile.health_state, "healthy")

    def test_discovery_reactivates_archived_currency(self):
        currency = (
            self.env["res.currency"]
            .sudo()
            .with_context(active_test=False)
            .search([("active", "=", False)], limit=1)
        )
        self.assertTrue(currency)
        account = MetaAdAccount(
            external_ref="act_990",
            external_id="990",
            name="Meta account with archived currency",
            currency=currency.name,
            timezone="America/Sao_Paulo",
            account_status=1,
            disable_reason=0,
        )
        service = self.env["marketing.center.meta.service"]
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.meta_service."
            "MetaMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class:
            adapter = adapter_class.return_value
            adapter.validate.return_value = self.validation
            adapter.discover_ad_accounts.return_value = (account,)
            result = service._discover_sources(
                self.profile,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.meta_app.revision,
            )
        source = self.env["marketing.center.source"].search(
            [("external_account_ref", "=", account.external_ref)], limit=1
        )
        self.assertEqual(result["discovered"], 1)
        self.assertTrue(currency.active)
        self.assertEqual(source.currency_id, currency)

    def test_discovery_reuses_and_reactivates_archived_source(self):
        service = self.env["marketing.center.meta.service"]
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.meta_service."
            "MetaMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class:
            adapter = adapter_class.return_value
            adapter.validate.return_value = self.validation
            adapter.discover_ad_accounts.return_value = (self.account,)
            service._discover_sources(
                self.profile,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.meta_app.revision,
            )
            source = self.env["marketing.center.source"].search(
                [("external_account_ref", "=", self.account.external_ref)], limit=1
            )
            source_id = source.id
            public_ref = source.public_ref
            source.write({"active": False})
            result = service._discover_sources(
                self.profile,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.meta_app.revision,
            )
        sources = (
            self.env["marketing.center.source"]
            .with_context(active_test=False)
            .search([("external_account_ref", "=", self.account.external_ref)])
        )
        self.assertEqual(result["discovered"], 1)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources.id, source_id)
        self.assertEqual(sources.public_ref, public_ref)
        self.assertTrue(sources.active)
        self.assertTrue(sources.read_enabled)
        self.assertEqual(sources.state, "active")

    def test_invalid_account_does_not_abort_discovery_batch(self):
        invalid = MetaAdAccount(
            external_ref="act_991",
            external_id="991",
            name="Meta account with invalid timezone",
            currency=self.env.company.currency_id.name,
            timezone="Invalid/Meta",
            account_status=1,
            disable_reason=0,
        )
        valid = MetaAdAccount(
            external_ref="act_992",
            external_id="992",
            name="Meta account projected after invalid account",
            currency=self.env.company.currency_id.name,
            timezone="America/Sao_Paulo",
            account_status=1,
            disable_reason=0,
        )
        service = self.env["marketing.center.meta.service"]
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.meta_service."
            "MetaMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class:
            adapter = adapter_class.return_value
            adapter.validate.return_value = self.validation
            adapter.discover_ad_accounts.return_value = (invalid, valid)
            result = service._discover_sources(
                self.profile,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.meta_app.revision,
            )
        source_model = self.env["marketing.center.source"]
        self.assertEqual(result["discovered"], 1)
        self.assertEqual(result["failed_accounts"], 1)
        self.assertFalse(
            source_model.search(
                [("external_account_ref", "=", invalid.external_ref)], limit=1
            )
        )
        self.assertTrue(
            source_model.search(
                [("external_account_ref", "=", valid.external_ref)], limit=1
            )
        )
        self.assertEqual(self.profile.health_state, "degraded")
        self.assertEqual(self.profile.last_error_class, "configuration")

    def test_rotated_profile_discards_provider_result_before_projection(self):
        service = self.env["marketing.center.meta.service"]
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.meta_service."
            "MetaMarketingReadAdapter"
        )
        expected_revision = self.profile.profile_revision
        expected_app_revision = self.meta_app.revision

        def rotate_during_validation():
            self.profile.write(
                {"access_token_ref": "ODOO_META_SERVICE_READER_TOKEN_ROTATED"}
            )
            return self.validation

        with patch(adapter_path) as adapter_class:
            adapter = adapter_class.return_value
            adapter.validate.side_effect = rotate_during_validation
            adapter.discover_ad_accounts.return_value = (self.account,)
            result = service._discover_sources(
                self.profile,
                expected_profile_revision=expected_revision,
                expected_app_revision=expected_app_revision,
            )
        self.assertEqual(result, {"discovered": 0, "stale": True})
        self.assertFalse(
            self.env["marketing.center.source"].search(
                [
                    ("service", "=", "meta.ads"),
                    ("external_account_ref", "=", "act_987"),
                ]
            )
        )
        self.assertEqual(self.profile.health_state, "unknown")

    def test_rotated_profile_discards_validation_health_result(self):
        service = self.env["marketing.center.meta.service"]
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.meta_service."
            "MetaMarketingReadAdapter"
        )
        expected_revision = self.profile.profile_revision
        expected_app_revision = self.meta_app.revision

        def rotate_during_validation():
            self.meta_app.write(
                {"app_secret_ref": "ODOO_META_SERVICE_APP_SECRET_ROTATED"}
            )
            return self.validation

        with patch(adapter_path) as adapter_class:
            adapter_class.return_value.validate.side_effect = rotate_during_validation
            result = service._validate_profile(
                self.profile,
                expected_profile_revision=expected_revision,
                expected_app_revision=expected_app_revision,
            )
        self.assertEqual(result, {"stale": True})
        self.assertEqual(self.profile.health_state, "unknown")

    def test_token_validation_does_not_certify_asset_connections(self):
        service = self.env["marketing.center.meta.service"]
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.meta_service."
            "MetaMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class:
            adapter = adapter_class.return_value
            adapter.validate.return_value = self.validation
            adapter.discover_ad_accounts.return_value = (self.account,)
            service._discover_sources(
                self.profile,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.meta_app.revision,
            )
            connection = self.env["marketing.center.connection"].search(
                [("source_id.external_account_ref", "=", "act_987")], limit=1
            )
            connection.write({"state": "paused"})
            snapshot = self._connection_snapshot(connection)
            service._validate_profile(
                self.profile,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.meta_app.revision,
            )
        self.assertEqual(self._connection_snapshot(connection), snapshot)
        self.assertEqual(self.profile.health_state, "healthy")

    def test_discovery_pauses_missing_account_and_recovers_when_returned(self):
        absent_account = MetaAdAccount(
            external_ref="act_989",
            external_id="989",
            name="Meta access revoked later",
            currency=self.env.company.currency_id.name,
            timezone="America/Sao_Paulo",
            account_status=1,
            disable_reason=0,
        )
        service = self.env["marketing.center.meta.service"]
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.meta_service."
            "MetaMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class:
            adapter = adapter_class.return_value
            adapter.validate.return_value = self.validation
            adapter.discover_ad_accounts.return_value = (
                self.account,
                absent_account,
            )
            service._discover_sources(
                self.profile,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.meta_app.revision,
            )
            absent = self.env["marketing.center.connection"].search(
                [("source_id.external_account_ref", "=", "act_989")], limit=1
            )
            source_id = absent.source_id.id
            public_ref = absent.source_id.public_ref
            adapter.discover_ad_accounts.return_value = (self.account,)
            missing = service._discover_sources(
                self.profile,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.meta_app.revision,
            )
            paused_source_state = absent.source_id.state
            paused_read_enabled = absent.source_id.read_enabled
            paused_connection_state = absent.state
            paused_health_state = absent.health_state
            paused_error_class = absent.last_health_error_class
            source_revision = absent.source_id.configuration_revision
            binding_revision = absent.binding_revision
            repeated = service._discover_sources(
                self.profile,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.meta_app.revision,
            )
            repeated_source_revision = absent.source_id.configuration_revision
            repeated_binding_revision = absent.binding_revision
            adapter.discover_ad_accounts.return_value = (
                self.account,
                absent_account,
            )
            returned = service._discover_sources(
                self.profile,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.meta_app.revision,
            )
        self.assertEqual(missing["missing"], 1)
        self.assertEqual(repeated["missing"], 1)
        self.assertEqual(returned["missing"], 0)
        self.assertEqual(paused_source_state, "attention")
        self.assertFalse(paused_read_enabled)
        self.assertEqual(paused_connection_state, "paused")
        self.assertEqual(paused_health_state, "degraded")
        self.assertEqual(paused_error_class, "meta_account_not_discovered")
        self.assertEqual(repeated_source_revision, source_revision)
        self.assertEqual(repeated_binding_revision, binding_revision)
        self.assertEqual(absent.source_id.id, source_id)
        self.assertEqual(absent.source_id.public_ref, public_ref)
        self.assertTrue(absent.source_id.active)
        self.assertTrue(absent.source_id.read_enabled)
        self.assertEqual(absent.source_id.state, "active")
        self.assertEqual(absent.state, "ready")
        self.assertEqual(absent.health_state, "healthy")
        self.assertFalse(absent.last_health_error_class)
        self.assertGreater(absent.source_id.configuration_revision, source_revision)
        self.assertGreater(absent.binding_revision, binding_revision)

    def test_missing_account_does_not_override_manual_connection_pause(self):
        account = MetaAdAccount(
            external_ref="act_993",
            external_id="993",
            name="Meta account paused by an administrator",
            currency=self.env.company.currency_id.name,
            timezone="America/Sao_Paulo",
            account_status=1,
            disable_reason=0,
        )
        service = self.env["marketing.center.meta.service"]
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.meta_service."
            "MetaMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class:
            adapter = adapter_class.return_value
            adapter.validate.return_value = self.validation
            adapter.discover_ad_accounts.return_value = (account,)
            service._discover_sources(
                self.profile,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.meta_app.revision,
            )
            connection = self.env["marketing.center.connection"].search(
                [("source_id.external_account_ref", "=", account.external_ref)],
                limit=1,
            )
            connection.write({"state": "paused"})
            manual_pause_revision = connection.binding_revision
            adapter.discover_ad_accounts.return_value = ()
            service._discover_sources(
                self.profile,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.meta_app.revision,
            )
            missing_pause_revision = connection.binding_revision
            missing_error_class = connection.last_health_error_class
            adapter.discover_ad_accounts.return_value = (account,)
            service._discover_sources(
                self.profile,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.meta_app.revision,
            )
        self.assertEqual(missing_pause_revision, manual_pause_revision)
        self.assertFalse(missing_error_class)
        self.assertEqual(connection.state, "paused")
        self.assertEqual(connection.binding_revision, manual_pause_revision)
        self.assertTrue(connection.source_id.read_enabled)
        self.assertEqual(connection.source_id.state, "active")

    def test_profile_changes_and_replay_never_resurrect_disabled_or_archived(self):
        other_account = MetaAdAccount(
            external_ref="act_988",
            external_id="988",
            name="Soloz Meta Archived",
            currency=self.env.company.currency_id.name,
            timezone="America/Sao_Paulo",
            account_status=1,
            disable_reason=0,
        )
        service = self.env["marketing.center.meta.service"]
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.meta_service."
            "MetaMarketingReadAdapter"
        )
        with patch(adapter_path) as adapter_class:
            adapter = adapter_class.return_value
            adapter.validate.return_value = self.validation
            adapter.discover_ad_accounts.return_value = (
                self.account,
                other_account,
            )
            service._discover_sources(
                self.profile,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.meta_app.revision,
            )

            disabled = self.env["marketing.center.connection"].search(
                [("source_id.external_account_ref", "=", "act_987")], limit=1
            )
            archived = self.env["marketing.center.connection"].search(
                [("source_id.external_account_ref", "=", "act_988")], limit=1
            )
            disabled.write({"state": "disabled"})
            archived.write({"active": False})
            before = {
                disabled.id: self._connection_snapshot(disabled),
                archived.id: self._connection_snapshot(archived),
            }
            source_revisions = {
                disabled.source_id.id: disabled.source_id.configuration_revision,
                archived.source_id.id: archived.source_id.configuration_revision,
            }

            self.profile.write(
                {"access_token_ref": "ODOO_META_SERVICE_READER_TOKEN_ROTATED"}
            )
            self.assertEqual(self._connection_snapshot(disabled), before[disabled.id])
            self.assertEqual(self._connection_snapshot(archived), before[archived.id])

            service._discover_sources(
                self.profile,
                expected_profile_revision=self.profile.profile_revision,
                expected_app_revision=self.meta_app.revision,
            )

        self.assertEqual(self._connection_snapshot(disabled), before[disabled.id])
        self.assertEqual(self._connection_snapshot(archived), before[archived.id])
        self.assertEqual(
            disabled.source_id.configuration_revision,
            source_revisions[disabled.source_id.id],
        )
        self.assertEqual(
            archived.source_id.configuration_revision,
            source_revisions[archived.source_id.id],
        )

    def _connection_snapshot(self, connection):
        return {
            "active": connection.active,
            "state": connection.state,
            "health_state": connection.health_state,
            "profile_revision": connection.profile_revision,
            "binding_revision": connection.binding_revision,
            "capabilities": connection.effective_capabilities_json,
            "capabilities_hash": connection.effective_capabilities_hash,
            "verified_at": connection.verified_at,
        }

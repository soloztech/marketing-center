from unittest.mock import patch

from odoo.tests.common import SavepointCase

from ..services.adapter import MetaAdAccount, MetaReadValidation


class TestMarketingCenterMetaService(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.profile = cls.env["marketing.center.meta.profile"].create(
            {
                "name": "Meta service laboratory",
                "company_id": cls.env.company.id,
                "external_app_id": "987654321",
                "credential_backend": "environment",
                "app_secret_ref": "ODOO_META_SERVICE_APP_SECRET",
                "access_token_ref": "ODOO_META_SERVICE_READER_TOKEN",
            }
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

    def test_rotated_profile_discards_provider_result_before_projection(self):
        service = self.env["marketing.center.meta.service"]
        adapter_path = (
            "odoo.addons.marketing_center_meta.models.meta_service."
            "MetaMarketingReadAdapter"
        )
        expected_revision = self.profile.profile_revision

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

        def rotate_during_validation():
            self.profile.write(
                {"app_secret_ref": "ODOO_META_SERVICE_APP_SECRET_ROTATED"}
            )
            return self.validation

        with patch(adapter_path) as adapter_class:
            adapter_class.return_value.validate.side_effect = rotate_during_validation
            result = service._validate_profile(
                self.profile,
                expected_profile_revision=expected_revision,
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
            )
            connection = self.env["marketing.center.connection"].search(
                [("source_id.external_account_ref", "=", "act_987")], limit=1
            )
            connection.write({"state": "paused"})
            snapshot = self._connection_snapshot(connection)
            service._validate_profile(
                self.profile,
                expected_profile_revision=self.profile.profile_revision,
            )
        self.assertEqual(self._connection_snapshot(connection), snapshot)
        self.assertEqual(self.profile.health_state, "healthy")

    def test_discovery_does_not_certify_account_absent_from_latest_result(self):
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
            )
            absent = self.env["marketing.center.connection"].search(
                [("source_id.external_account_ref", "=", "act_989")], limit=1
            )
            absent.write({"state": "paused"})
            snapshot = self._connection_snapshot(absent)
            adapter.discover_ad_accounts.return_value = (self.account,)
            service._discover_sources(
                self.profile,
                expected_profile_revision=self.profile.profile_revision,
            )
        self.assertEqual(self._connection_snapshot(absent), snapshot)

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

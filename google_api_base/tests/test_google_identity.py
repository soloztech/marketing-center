import json
import os
import pickle
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from ..services import GOOGLE_API_RUNTIME_CONTEXT_KEY, GOOGLE_API_RUNTIME_TOKEN
from ..services.credentials import (
    GoogleCredentialResolutionError,
    GoogleRuntimeIdentity,
)


class TestGoogleApiIdentity(SavepointCase):
    ENVIRONMENT = {
        "ODOO_GOOGLE_CLIENT_ID": "synthetic-client-id.apps.googleusercontent.com",
        "ODOO_GOOGLE_CLIENT_SECRET": "synthetic-client-secret",
        "ODOO_GOOGLE_REFRESH_TOKEN": "synthetic-refresh-token",
        "ODOO_GOOGLE_DEVELOPER_TOKEN": "synthetic-developer-token",
    }

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.identity = cls.env["google.api.identity"].create(
            {
                "name": "Google Ads laboratory reader",
                "api_version": "v25",
                "credential_backend": "environment",
                "oauth_client_id_ref": "ODOO_GOOGLE_CLIENT_ID",
                "oauth_client_secret_ref": "ODOO_GOOGLE_CLIENT_SECRET",
                "refresh_token_ref": "ODOO_GOOGLE_REFRESH_TOKEN",
                "developer_token_ref": "ODOO_GOOGLE_DEVELOPER_TOKEN",
                "login_customer_id": "123-456-7890",
            }
        )

    def _runtime_record(self):
        return self.identity.with_context(
            **{GOOGLE_API_RUNTIME_CONTEXT_KEY: GOOGLE_API_RUNTIME_TOKEN}
        )

    def test_model_persists_only_references_and_returns_fenced_runtime(self):
        forbidden_fields = {
            "oauth_client_id",
            "oauth_client_secret",
            "refresh_token",
            "developer_token",
            "access_token",
        }
        self.assertFalse(forbidden_fields.intersection(self.identity._fields))
        with patch.dict(os.environ, self.ENVIRONMENT, clear=False):
            runtime = self._runtime_record()._resolve_runtime(
                expected_revision=self.identity.revision
            )

        self.assertIsInstance(runtime, GoogleRuntimeIdentity)
        self.assertEqual(runtime.login_customer_id, "1234567890")
        self.assertEqual(runtime.company_id, self.env.company.id)
        self.assertEqual(runtime.public_ref, self.identity.public_ref)
        for credential in self.ENVIRONMENT.values():
            self.assertNotIn(credential, repr(runtime))
        with self.assertRaises(TypeError):
            pickle.dumps(runtime)

    def test_runtime_capability_rejects_rpc_and_serialization(self):
        with patch(
            "odoo.addons.google_api_base.models.google_identity.resolve_credential"
        ) as resolver:
            with self.assertRaises(AccessError):
                self.identity._resolve_runtime(self.identity.revision)
            with self.assertRaises(AccessError):
                self.identity.with_context(
                    **{GOOGLE_API_RUNTIME_CONTEXT_KEY: True}
                )._resolve_runtime(self.identity.revision)
        resolver.assert_not_called()
        with self.assertRaises(TypeError):
            json.dumps({GOOGLE_API_RUNTIME_CONTEXT_KEY: GOOGLE_API_RUNTIME_TOKEN})

    def test_configuration_writes_are_revisioned_and_stale_jobs_are_fenced(self):
        initial = self.identity.revision
        self.identity.write({"name": "Renamed Google Ads laboratory reader"})
        self.assertEqual(self.identity.revision, initial)
        self.identity.write({"login_customer_id": "123-456-7890"})
        self.assertEqual(self.identity.revision, initial)
        self.identity.write({"login_customer_id": "234-567-8901"})
        self.assertEqual(self.identity.revision, initial + 1)
        with patch(
            "odoo.addons.google_api_base.models.google_identity.resolve_credential"
        ) as resolver, self.assertRaisesRegex(
            GoogleCredentialResolutionError, "configuration changed"
        ):
            self._runtime_record()._resolve_runtime(initial)
        resolver.assert_not_called()

    def test_identity_and_company_are_immutable(self):
        with self.assertRaises(AccessError):
            self.identity.write({"public_ref": self.identity.public_ref})
        with self.assertRaises(AccessError):
            self.env["google.api.identity"].create(
                {
                    "name": "Injected identity",
                    "public_ref": self.identity.public_ref,
                    "credential_backend": "environment",
                    "oauth_client_id_ref": "ODOO_GOOGLE_CLIENT_ID",
                    "oauth_client_secret_ref": "ODOO_GOOGLE_CLIENT_SECRET",
                    "refresh_token_ref": "ODOO_GOOGLE_REFRESH_TOKEN",
                    "developer_token_ref": "ODOO_GOOGLE_DEVELOPER_TOKEN",
                }
            )
        other_company = self.env["res.company"].create({"name": "Other Google company"})
        with self.assertRaises(AccessError):
            self.identity.write({"company_id": other_company.id})

    def test_invalid_configuration_fails_closed(self):
        base_values = {
            "name": "Invalid Google identity",
            "credential_backend": "environment",
            "oauth_client_id_ref": "ODOO_GOOGLE_CLIENT_ID",
            "oauth_client_secret_ref": "ODOO_GOOGLE_CLIENT_SECRET",
            "refresh_token_ref": "ODOO_GOOGLE_REFRESH_TOKEN",
            "developer_token_ref": "ODOO_GOOGLE_DEVELOPER_TOKEN",
        }
        for values in (
            {"api_version": "latest"},
            {"api_version": "v26"},
            {"login_customer_id": "123"},
            {"login_customer_id": "１２３４５６７８９０"},
            {"oauth_client_id_ref": "literal.apps.googleusercontent.com"},
            {"credential_backend": "file", "refresh_token_ref": "../token"},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.env["google.api.identity"].create({**base_values, **values})
        for expected_revision in (None, True, 0, "1"):
            with self.subTest(expected_revision=expected_revision), self.assertRaises(
                GoogleCredentialResolutionError
            ):
                self._runtime_record()._resolve_runtime(expected_revision)

    def test_paused_identity_does_not_resolve_credentials(self):
        self.identity.write({"active": False})
        with patch(
            "odoo.addons.google_api_base.models.google_identity.resolve_credential"
        ) as resolver, self.assertRaisesRegex(
            GoogleCredentialResolutionError, "paused"
        ):
            self._runtime_record()._resolve_runtime(self.identity.revision)
        resolver.assert_not_called()

    def test_service_account_identity_resolves_only_the_selected_flow(self):
        identity = self.env["google.api.identity"].create(
            {
                "name": "Google Ads service account reader",
                "auth_mode": "service_account",
                "credential_backend": "file",
                "service_account_json_ref": "google-reader.json",
                "developer_token_ref": "google-developer-token",
            }
        )
        service_info = {
            "type": "service_account",
            "client_email": "reader@example.iam.gserviceaccount.com",
        }
        runtime_record = identity.with_context(
            **{GOOGLE_API_RUNTIME_CONTEXT_KEY: GOOGLE_API_RUNTIME_TOKEN}
        )
        with patch(
            "odoo.addons.google_api_base.models.google_identity.resolve_credential",
            return_value="synthetic-developer-token",
        ) as scalar_resolver, patch(
            "odoo.addons.google_api_base.models.google_identity.resolve_service_account_info",
            return_value=service_info,
        ) as json_resolver:
            runtime = runtime_record._resolve_runtime(identity.revision)

        self.assertEqual(runtime.auth_mode, "service_account")
        self.assertEqual(runtime.service_account_info, service_info)
        self.assertFalse(runtime.oauth_client_id)
        scalar_resolver.assert_called_once_with("file", "google-developer-token")
        json_resolver.assert_called_once_with("file", "google-reader.json")
        self.assertNotIn("synthetic-developer-token", repr(runtime))
        self.assertNotIn("reader@example", repr(runtime))

    def test_runtime_snapshot_does_not_lock_identity_across_provider_io(self):
        cursor_class = type(self.env.cr)
        original_execute = cursor_class.execute
        observed_queries = []

        def traced_execute(cursor, query, *args, **kwargs):
            normalized = " ".join(str(query).upper().split())
            if "FROM GOOGLE_API_IDENTITY" in normalized:
                observed_queries.append(normalized)
            return original_execute(cursor, query, *args, **kwargs)

        with patch.object(cursor_class, "execute", traced_execute), patch(
            "odoo.addons.google_api_base.models.google_identity.resolve_credential",
            return_value="synthetic-secret",
        ):
            self._runtime_record()._resolve_runtime(self.identity.revision)

        self.assertTrue(observed_queries)
        self.assertFalse(
            any(
                "FOR SHARE" in query or "FOR UPDATE" in query
                for query in observed_queries
            )
        )

    def test_authentication_mode_requires_its_own_references(self):
        common = {
            "name": "Incomplete Google identity",
            "credential_backend": "environment",
            "developer_token_ref": "ODOO_GOOGLE_DEVELOPER_TOKEN",
        }
        for values in (
            {"auth_mode": "authorized_user"},
            {"auth_mode": "service_account"},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.env["google.api.identity"].create({**common, **values})

    def test_acl_and_company_rule_limit_system_administrators(self):
        regular = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Google API regular user",
                    "login": "google-api-regular-user",
                    "company_id": self.env.company.id,
                    "company_ids": [Command.set(self.env.company.ids)],
                    "groups_id": [Command.set(self.env.ref("base.group_user").ids)],
                }
            )
        )
        with self.assertRaises(AccessError):
            self.env["google.api.identity"].with_user(regular).check_access_rights(
                "read"
            )

        other_company = self.env["res.company"].create(
            {"name": "Google API isolated company"}
        )
        other_identity = self.identity.copy(
            {"name": "Isolated Google identity", "company_id": other_company.id}
        )
        administrator = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Google API company administrator",
                    "login": "google-api-company-administrator",
                    "company_id": self.env.company.id,
                    "company_ids": [Command.set(self.env.company.ids)],
                    "groups_id": [Command.set(self.env.ref("base.group_system").ids)],
                }
            )
        )
        model = self.env["google.api.identity"].with_user(administrator)
        self.assertEqual(set(model.search([]).ids), set(self.identity.ids))
        with self.assertRaises(AccessError):
            other_identity.with_user(administrator).check_access_rule("read")

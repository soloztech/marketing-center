import json
import os
import pickle
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from ..services import META_API_RUNTIME_CONTEXT_KEY, META_API_RUNTIME_TOKEN
from ..services.credentials import MetaCredentialResolutionError, MetaRuntimeApp
from ..services.graph import graph_app_access_token


class TestMetaApiApp(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = cls.env["meta.api.app"].create(
            {
                "name": "Shared Meta laboratory App",
                "external_app_id": "100000000000001",
                "graph_version": "v26.0",
                "credential_backend": "environment",
                "app_secret_ref": "ODOO_META_SHARED_APP_SECRET",
            }
        )

    def _runtime_app(self):
        return self.app.with_context(
            **{META_API_RUNTIME_CONTEXT_KEY: META_API_RUNTIME_TOKEN}
        )

    def test_runtime_capability_rejects_rpc_contexts(self):
        with patch(
            "odoo.addons.meta_api_base.models.meta_app.resolve_secret"
        ) as resolver:
            with self.assertRaises(AccessError):
                self.app._resolve_runtime(expected_revision=self.app.revision)
            with self.assertRaises(AccessError):
                self.app.with_context(
                    **{META_API_RUNTIME_CONTEXT_KEY: True}
                )._resolve_runtime(expected_revision=self.app.revision)
        resolver.assert_not_called()

    def test_runtime_capability_is_not_serializable(self):
        with self.assertRaises(TypeError):
            json.dumps({META_API_RUNTIME_CONTEXT_KEY: META_API_RUNTIME_TOKEN})

    def test_model_persists_only_reference_and_returns_fenced_runtime(self):
        self.assertNotIn("app_secret", self.app._fields)
        self.assertNotIn("access_token", self.app._fields)
        revision = self.app.revision
        secret = "synthetic-meta-app-secret"
        with patch.dict(
            os.environ,
            {"ODOO_META_SHARED_APP_SECRET": secret},
            clear=False,
        ):
            runtime = self._runtime_app()._resolve_runtime(expected_revision=revision)

        self.assertIsInstance(runtime, MetaRuntimeApp)
        self.assertEqual(runtime.public_ref, self.app.public_ref)
        self.assertEqual(runtime.revision, revision)
        self.assertEqual(runtime.company_id, self.app.company_id.id)
        self.assertNotIn(secret, repr(runtime))
        with self.assertRaises(TypeError):
            pickle.dumps(runtime)
        self.assertEqual(
            graph_app_access_token(runtime),
            "%s|%s" % (self.app.external_app_id, secret),
        )

    def test_configuration_writes_advance_revision_and_fence_stale_jobs(self):
        initial_revision = self.app.revision
        self.app.write({"name": "Renamed shared Meta App"})
        self.assertEqual(self.app.revision, initial_revision)

        self.app.write({"graph_version": self.app.graph_version})
        self.assertEqual(self.app.revision, initial_revision)

        self.app.write({"graph_version": "v25.0"})
        self.assertEqual(self.app.revision, initial_revision + 1)
        with patch(
            "odoo.addons.meta_api_base.models.meta_app.resolve_secret"
        ) as resolver, self.assertRaisesRegex(
            MetaCredentialResolutionError,
            "configuration changed",
        ):
            self._runtime_app()._resolve_runtime(expected_revision=initial_revision)
        resolver.assert_not_called()

    def test_public_identity_and_company_are_immutable(self):
        with self.assertRaises(AccessError):
            self.app.write({"public_ref": self.app.public_ref})
        with self.assertRaises(AccessError):
            self.env["meta.api.app"].create(
                {
                    "name": "Injected identity",
                    "public_ref": self.app.public_ref,
                    "external_app_id": "100000000000002",
                    "credential_backend": "environment",
                    "app_secret_ref": "ODOO_META_SHARED_APP_SECRET",
                }
            )

        other_company = self.env["res.company"].create({"name": "Other Meta company"})
        with self.assertRaises(AccessError):
            self.app.write({"company_id": other_company.id})

    def test_invalid_configuration_and_expected_revision_fail_closed(self):
        for values in (
            {"external_app_id": "not-numeric"},
            {"external_app_id": "１０００００００００００００１"},
            {"graph_version": "latest"},
            {"credential_backend": "file", "app_secret_ref": "../secret"},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.env["meta.api.app"].create(
                    {
                        "name": "Invalid shared Meta App",
                        "external_app_id": "100000000000099",
                        "credential_backend": "environment",
                        "app_secret_ref": "ODOO_META_SHARED_APP_SECRET",
                        **values,
                    }
                )
        for expected_revision in (True, 0, "1"):
            with self.subTest(expected_revision=expected_revision), self.assertRaises(
                MetaCredentialResolutionError
            ):
                self._runtime_app()._resolve_runtime(
                    expected_revision=expected_revision
                )

    def test_paused_app_does_not_resolve_the_external_secret(self):
        initial_revision = self.app.revision
        self.app.write({"active": False})
        current_revision = self.app.revision
        self.assertEqual(current_revision, initial_revision + 1)
        with patch(
            "odoo.addons.meta_api_base.models.meta_app.resolve_secret"
        ) as resolver, self.assertRaisesRegex(
            MetaCredentialResolutionError,
            "paused",
        ):
            self._runtime_app()._resolve_runtime(expected_revision=current_revision)
        resolver.assert_not_called()

    def test_acl_is_limited_to_system_administrators(self):
        user = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Meta API regular user",
                    "login": "meta-api-regular-user",
                    "company_id": self.env.company.id,
                    "company_ids": [Command.set(self.env.company.ids)],
                    "groups_id": [Command.set(self.env.ref("base.group_user").ids)],
                }
            )
        )
        with self.assertRaises(AccessError):
            self.env["meta.api.app"].with_user(user).check_access_rights("read")

    def test_system_administrator_is_limited_to_active_companies(self):
        other_company = self.env["res.company"].create(
            {"name": "Meta API isolated company"}
        )
        other_app = self.env["meta.api.app"].create(
            {
                "name": "Isolated Meta App",
                "company_id": other_company.id,
                "external_app_id": "100000000000003",
                "credential_backend": "environment",
                "app_secret_ref": "ODOO_META_SHARED_APP_SECRET",
            }
        )
        administrator = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Meta API company administrator",
                    "login": "meta-api-company-administrator",
                    "company_id": self.env.company.id,
                    "company_ids": [Command.set(self.env.company.ids)],
                    "groups_id": [Command.set(self.env.ref("base.group_system").ids)],
                }
            )
        )
        app_model = self.env["meta.api.app"].with_user(administrator)

        self.assertEqual(set(app_model.search([]).ids), set(self.app.ids))
        with self.assertRaises(AccessError):
            other_app.with_user(administrator).check_access_rule("read")

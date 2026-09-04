import uuid

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.tokens import (
    MARKETING_META_CONNECTION_TOKEN,
    MARKETING_META_PROFILE_RUNTIME_TOKEN,
)
from .common import create_meta_profile


class TestMarketingCenterMetaProfile(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.meta_app, cls.profile = create_meta_profile(
            cls.env,
            name="Meta reader laboratory",
            external_app_id="123456789",
            app_secret_ref="ODOO_META_APP_SECRET",
            access_token_ref="ODOO_META_READER_TOKEN",
            required_scopes="leads_retrieval, ads_read,ads_read",
        )
        cls.source = cls.env["marketing.center.source"].create(
            {
                "name": "Meta source",
                "company_id": cls.env.company.id,
                "service": "meta.ads",
                "external_account_ref": "act_123",
                "currency_id": cls.env.company.currency_id.id,
                "timezone": "UTC",
                "state": "active",
            }
        )

    def test_profile_stores_only_references_and_normalizes_scopes(self):
        self.assertEqual(self.profile.required_scopes, "ads_read,leads_retrieval")
        serialized = str(self.profile.read()[0])
        self.assertNotIn("synthetic-meta-app-secret", serialized)
        self.assertNotIn("synthetic-meta-reader-token", serialized)
        with self.assertRaises(ValidationError):
            self.env["marketing.center.meta.profile"].create(
                {
                    "name": "Invalid ref",
                    "meta_app_id": self.meta_app.id,
                    "access_token_ref": "actual token value",
                }
            )
        with self.assertRaises(AccessError):
            self.profile.write({"public_ref": str(uuid.uuid4())})
        with self.assertRaises(AccessError):
            self.env["marketing.center.meta.profile"].create(
                {
                    "name": "Caller supplied identity",
                    "public_ref": str(uuid.uuid4()),
                    "meta_app_id": self.meta_app.id,
                    "access_token_ref": "ODOO_META_READER_TOKEN_OTHER",
                }
            )

    def test_meta_connection_binding_is_reader_only_and_internal_ready(self):
        values = {
            "name": "Meta reader",
            "source_id": self.source.id,
            "adapter_key": "meta.graph",
            "purpose": "reader",
            "meta_profile_id": self.profile.id,
            "state": "draft",
        }
        connection = self.env["marketing.center.connection"].create(values)
        self.assertEqual(connection.profile_public_ref, self.profile.public_ref)
        self.assertEqual(connection.profile_revision, self.profile.profile_revision)
        with self.assertRaises(AccessError):
            connection.write({"state": "ready"})
        connection.with_context(
            marketing_meta_connection_token=MARKETING_META_CONNECTION_TOKEN
        ).write({"state": "ready"})
        self.assertEqual(connection.state, "ready")
        with self.assertRaises(ValidationError):
            self.env["marketing.center.connection"].create(
                dict(values, name="Meta writer", purpose="writer")
            )

    def test_profile_rotation_fences_and_pauses_connections(self):
        connection = self.env["marketing.center.connection"].create(
            {
                "name": "Meta rotating reader",
                "source_id": self.source.id,
                "adapter_key": "meta.graph",
                "purpose": "reader",
                "meta_profile_id": self.profile.id,
                "state": "draft",
            }
        )
        old_profile_revision = self.profile.profile_revision
        connection.with_context(
            marketing_meta_connection_token=MARKETING_META_CONNECTION_TOKEN
        ).write({"state": "ready"})
        self.profile.write({"access_token_ref": "ODOO_META_READER_TOKEN_V2"})
        self.assertEqual(self.profile.profile_revision, old_profile_revision + 1)
        self.assertEqual(connection.state, "paused")
        self.assertLess(connection.profile_revision, self.profile.profile_revision)
        with self.assertRaises(ValidationError):
            connection.with_context(
                marketing_meta_connection_token=MARKETING_META_CONNECTION_TOKEN
            ).write({"state": "ready"})

    def test_profile_noop_write_preserves_revision_health_and_connections(self):
        connection = self.env["marketing.center.connection"].create(
            {
                "name": "Meta stable reader",
                "source_id": self.source.id,
                "adapter_key": "meta.graph",
                "purpose": "reader",
                "meta_profile_id": self.profile.id,
                "state": "draft",
            }
        )
        connection.with_context(
            marketing_meta_connection_token=MARKETING_META_CONNECTION_TOKEN
        ).write({"state": "ready"})
        self.profile.with_context(
            marketing_meta_profile_runtime_token=MARKETING_META_PROFILE_RUNTIME_TOKEN
        ).write({"health_state": "healthy"})
        revision = self.profile.profile_revision

        self.profile.write(
            {
                "access_token_ref": "  %s  " % self.profile.access_token_ref,
                "reader_kind": self.profile.reader_kind,
            }
        )
        self.profile.invalidate_recordset(["profile_revision", "health_state"])
        connection.invalidate_recordset(["state"])

        self.assertEqual(self.profile.profile_revision, revision)
        self.assertEqual(self.profile.health_state, "healthy")
        self.assertEqual(connection.state, "ready")
        self.assertEqual(
            self.profile.required_scopes,
            "ads_read,leads_retrieval",
        )

    def test_meta_connection_rejects_non_meta_source(self):
        source = self.env["marketing.center.source"].create(
            {
                "name": "Manual source",
                "company_id": self.env.company.id,
                "service": "manual.import",
                "external_account_ref": "manual-source",
                "currency_id": self.env.company.currency_id.id,
                "timezone": "UTC",
                "state": "active",
            }
        )
        with self.assertRaises(ValidationError):
            self.env["marketing.center.connection"].create(
                {
                    "name": "Invalid Meta reader",
                    "source_id": source.id,
                    "adapter_key": "meta.graph",
                    "purpose": "reader",
                    "meta_profile_id": self.profile.id,
                    "state": "draft",
                }
            )

    def test_bound_meta_source_service_identity_is_immutable(self):
        self.env["marketing.center.connection"].create(
            {
                "name": "Meta identity reader",
                "source_id": self.source.id,
                "adapter_key": "meta.graph",
                "purpose": "reader",
                "meta_profile_id": self.profile.id,
                "state": "draft",
            }
        )
        with self.assertRaises(AccessError):
            self.source.write({"service": "manual.import"})
        with self.assertRaises(AccessError):
            self.source.write({"external_account_ref": "act_999"})

    def test_ui_actions_enqueue_only_revision_fenced_jobs(self):
        with trap_jobs() as validation_trap:
            self.profile.action_enqueue_validation()
            validation_trap.assert_jobs_count(1)
            validation_job = validation_trap.enqueued_jobs[0]
        self.assertEqual(
            validation_job.kwargs,
            {
                "expected_profile_revision": self.profile.profile_revision,
                "expected_app_revision": self.meta_app.revision,
            },
        )
        self.assertNotIn(self.meta_app.app_secret_ref, repr(validation_job.kwargs))
        self.assertNotIn(self.profile.access_token_ref, repr(validation_job.kwargs))

        with trap_jobs() as discovery_trap:
            self.profile.action_enqueue_discovery()
            discovery_trap.assert_jobs_count(1)
            discovery_job = discovery_trap.enqueued_jobs[0]
        self.assertEqual(
            discovery_job.kwargs,
            {
                "expected_profile_revision": self.profile.profile_revision,
                "expected_app_revision": self.meta_app.revision,
            },
        )

    def test_profile_acl_is_admin_only_and_company_scoped(self):
        admin_group = self.env.ref("marketing_center_base.group_marketing_center_admin")
        marketing_admin = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Meta marketing administrator",
                    "login": "meta-marketing-admin-%s" % uuid.uuid4(),
                    "company_id": self.env.company.id,
                    "company_ids": [Command.set(self.env.company.ids)],
                    "groups_id": [Command.set(admin_group.ids)],
                }
            )
        )
        app_model = self.env["meta.api.app"].with_user(marketing_admin)
        self.assertTrue(app_model.check_access_rights("read"))
        self.assertEqual(
            app_model.search([("id", "=", self.meta_app.id)]), self.meta_app
        )
        with self.assertRaises(AccessError):
            app_model.check_access_rights("create")
        profile_model = self.env["marketing.center.meta.profile"].with_user(
            marketing_admin
        )
        self.assertEqual(
            profile_model.search([("id", "=", self.profile.id)]),
            self.profile,
        )
        with self.assertRaises(AccessError):
            self.profile.with_user(marketing_admin).write(
                {"access_token_ref": "ODOO_META_FORBIDDEN_SECRET_REFERENCE"}
            )
        with self.assertRaises(AccessError):
            profile_model.create(
                {
                    "name": "Forbidden credential selector",
                    "meta_app_id": self.meta_app.id,
                    "access_token_ref": "ODOO_META_FORBIDDEN_SECRET_REFERENCE",
                }
            )

        viewer_group = self.env.ref(
            "marketing_center_base.group_marketing_center_viewer"
        )
        viewer = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Meta profile viewer",
                    "login": "meta-profile-viewer-%s" % uuid.uuid4(),
                    "company_id": self.env.company.id,
                    "company_ids": [Command.set(self.env.company.ids)],
                    "groups_id": [Command.set(viewer_group.ids)],
                }
            )
        )
        with self.assertRaises(AccessError):
            self.env["marketing.center.meta.profile"].with_user(
                viewer
            ).check_access_rights("read")
        with self.assertRaises(AccessError):
            self.env["meta.api.app"].with_user(viewer).check_access_rights("read")
        other_company = self.env["res.company"].create(
            {"name": "Meta profile other company %s" % uuid.uuid4()}
        )
        other_app = (
            self.env["meta.api.app"]
            .sudo()
            .create(
                {
                    "name": "Other company Meta App",
                    "company_id": other_company.id,
                    "external_app_id": "9988776655",
                    "credential_backend": "environment",
                    "app_secret_ref": "ODOO_META_OTHER_COMPANY_APP_SECRET",
                }
            )
        )
        self.assertFalse(app_model.search([("id", "=", other_app.id)]))
        with self.assertRaises(AccessError):
            self.profile.write({"company_id": other_company.id})
        with self.assertRaises(AccessError):
            self.profile.unlink()

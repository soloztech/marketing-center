import uuid

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.tokens import MARKETING_META_CONNECTION_TOKEN


class TestMarketingCenterMetaProfile(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.profile = cls.env["marketing.center.meta.profile"].create(
            {
                "name": "Meta reader laboratory",
                "company_id": cls.env.company.id,
                "external_app_id": "123456789",
                "credential_backend": "environment",
                "app_secret_ref": "ODOO_META_APP_SECRET",
                "access_token_ref": "ODOO_META_READER_TOKEN",
                "required_scopes": "leads_retrieval, ads_read,ads_read",
            }
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
                    "external_app_id": "777",
                    "app_secret_ref": "actual secret value",
                    "access_token_ref": "ODOO_META_READER_TOKEN",
                }
            )
        with self.assertRaises(AccessError):
            self.profile.write({"public_ref": str(uuid.uuid4())})
        with self.assertRaises(AccessError):
            self.env["marketing.center.meta.profile"].create(
                {
                    "name": "Caller supplied identity",
                    "public_ref": str(uuid.uuid4()),
                    "external_app_id": "778",
                    "app_secret_ref": "ODOO_META_APP_SECRET_OTHER",
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
            {"expected_profile_revision": self.profile.profile_revision},
        )
        self.assertNotIn(self.profile.app_secret_ref, repr(validation_job.kwargs))
        self.assertNotIn(self.profile.access_token_ref, repr(validation_job.kwargs))

        with trap_jobs() as discovery_trap:
            self.profile.action_enqueue_discovery()
            discovery_trap.assert_jobs_count(1)
            discovery_job = discovery_trap.enqueued_jobs[0]
        self.assertEqual(
            discovery_job.kwargs,
            {"expected_profile_revision": self.profile.profile_revision},
        )

    def test_profile_acl_is_admin_only_and_company_scoped(self):
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
        other_company = self.env["res.company"].create(
            {"name": "Meta profile other company %s" % uuid.uuid4()}
        )
        with self.assertRaises(AccessError):
            self.profile.write({"company_id": other_company.id})
        with self.assertRaises(AccessError):
            self.profile.unlink()

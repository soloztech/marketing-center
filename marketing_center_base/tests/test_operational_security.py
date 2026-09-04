import datetime
import uuid

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from ..services.catalog_dto import ExternalEntityDTO
from ..services.performance_dto import MarketingPerformanceDTO


class TestMarketingOperationalSecurity(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.viewer_group = cls.env.ref(
            "marketing_center_base.group_marketing_center_viewer"
        )
        cls.admin_group = cls.env.ref(
            "marketing_center_base.group_marketing_center_admin"
        )
        cls.analyst_group = cls.env.ref(
            "marketing_center_base.group_marketing_center_analyst"
        )
        cls.viewer = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Marketing Roster Viewer",
                    "login": "marketing-viewer-%s" % uuid.uuid4(),
                    "email": "marketing-viewer@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [Command.set(cls.env.company.ids)],
                    "groups_id": [Command.set(cls.viewer_group.ids)],
                }
            )
        )
        cls.marketing_admin = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Marketing Company Administrator",
                    "login": "marketing-admin-%s" % uuid.uuid4(),
                    "email": "marketing-admin@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [Command.set(cls.env.company.ids)],
                    "groups_id": [Command.set(cls.admin_group.ids)],
                }
            )
        )
        cls.visible_source = cls._create_source("Visible", "account-visible")
        cls.hidden_source = cls._create_source("Hidden", "account-hidden")
        cls.team = cls.env["marketing.center.team"].create(
            {"name": "Viewer roster", "company_id": cls.env.company.id}
        )
        cls.env["marketing.center.team.member"].create(
            {
                "team_id": cls.team.id,
                "user_id": cls.viewer.id,
                "role": "viewer",
            }
        )
        cls.env["marketing.center.team.source"].create(
            {
                "team_id": cls.team.id,
                "source_id": cls.visible_source.id,
                "access_mode": "read",
            }
        )
        cls.visible_entity = cls._create_entity(cls.visible_source, "campaign-visible")
        cls.hidden_entity = cls._create_entity(cls.hidden_source, "campaign-hidden")
        cls.visible_metric = cls._create_metric(cls.visible_source, "campaign-visible")
        cls.hidden_metric = cls._create_metric(cls.hidden_source, "campaign-hidden")

    @classmethod
    def _create_source(cls, name, external_ref):
        return cls.env["marketing.center.source"].create(
            {
                "name": name,
                "company_id": cls.env.company.id,
                "service": "manual.import",
                "external_account_ref": external_ref,
                "currency_id": cls.env.company.currency_id.id,
                "timezone": "UTC",
                "state": "active",
            }
        )

    @classmethod
    def _create_entity(cls, source, external_ref):
        result = cls.env["marketing.center.catalog.service"]._upsert_entity(
            cls.env.company,
            source,
            ExternalEntityDTO(
                entity_type="campaign",
                external_ref=external_ref,
                name=external_ref,
                observed_at=datetime.datetime(2026, 8, 30, 14, 0),
            ),
        )
        return cls.env["marketing.center.external.entity"].browse(result.entity_id)

    @classmethod
    def _create_metric(cls, source, external_ref):
        result = cls.env["marketing.center.performance.service"]._upsert_metric(
            cls.env.company,
            source,
            MarketingPerformanceDTO(
                grain="campaign",
                entity_external_ref=external_ref,
                report_date=datetime.date(2026, 8, 30),
                period_start_utc=datetime.datetime(2026, 8, 30, 0, 0),
                period_end_utc=datetime.datetime(2026, 8, 31, 0, 0),
                report_timezone="UTC",
                currency=source.currency_id.name,
                observed_at=datetime.datetime(2026, 8, 31, 12, 0),
                impressions=10,
            ),
        )
        return cls.env["marketing.center.metric.daily"].browse(result.metric_id)

    def test_roster_scopes_sources_entities_and_revisions(self):
        viewer_env = self.env(user=self.viewer)
        sources = viewer_env["marketing.center.source"].search([])
        self.assertEqual(sources, self.visible_source)
        entities = viewer_env["marketing.center.external.entity"].search([])
        self.assertEqual(entities, self.visible_entity)
        revisions = viewer_env["marketing.center.external.entity.revision"].search([])
        self.assertEqual(revisions.entity_id, self.visible_entity)
        metrics = viewer_env["marketing.center.metric.daily"].search([])
        self.assertEqual(metrics, self.visible_metric)
        metric_revisions = viewer_env["marketing.center.metric.revision"].search([])
        self.assertEqual(metric_revisions.metric_id, self.visible_metric)
        with self.assertRaises(AccessError):
            self.hidden_source.with_user(self.viewer).check_access_rule("read")
        with self.assertRaises(AccessError):
            self.hidden_entity.with_user(self.viewer).check_access_rule("read")
        with self.assertRaises(AccessError):
            self.hidden_metric.with_user(self.viewer).check_access_rule("read")
        admin_metrics = (
            self.env["marketing.center.metric.daily"]
            .with_user(self.marketing_admin)
            .search([("id", "in", [self.visible_metric.id, self.hidden_metric.id])])
        )
        self.assertEqual(
            set(admin_metrics.ids),
            {self.visible_metric.id, self.hidden_metric.id},
        )

    def test_two_team_rosters_do_not_cross_source_boundaries(self):
        second_viewer = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Second marketing roster viewer",
                    "login": "marketing-second-viewer-%s" % uuid.uuid4(),
                    "company_id": self.env.company.id,
                    "company_ids": [Command.set(self.env.company.ids)],
                    "groups_id": [Command.set(self.viewer_group.ids)],
                }
            )
        )
        second_team = self.env["marketing.center.team"].create(
            {"name": "Second source roster", "company_id": self.env.company.id}
        )
        self.env["marketing.center.team.member"].create(
            {
                "team_id": second_team.id,
                "user_id": second_viewer.id,
                "role": "viewer",
            }
        )
        self.env["marketing.center.team.source"].create(
            {
                "team_id": second_team.id,
                "source_id": self.hidden_source.id,
                "access_mode": "read",
            }
        )

        first_sources = (
            self.env["marketing.center.source"].with_user(self.viewer).search([])
        )
        second_sources = (
            self.env["marketing.center.source"].with_user(second_viewer).search([])
        )
        self.assertEqual(first_sources.ids, self.visible_source.ids)
        self.assertEqual(second_sources.ids, self.hidden_source.ids)

    def test_technical_fields_and_connections_remain_admin_only(self):
        viewer_source_fields = (
            self.env["marketing.center.source"].with_user(self.viewer).fields_get()
        )
        self.assertNotIn("effective_capabilities_json", viewer_source_fields)
        viewer_revision_fields = (
            self.env["marketing.center.metric.revision"]
            .with_user(self.viewer)
            .fields_get()
        )
        self.assertNotIn("snapshot_json", viewer_revision_fields)
        with self.assertRaises(AccessError):
            self.env["marketing.center.connection"].with_user(
                self.viewer
            ).check_access_rights("read")
        self.assertIn(
            self.admin_group,
            self.env.ref("base.group_system").implied_ids,
        )

    def test_team_source_cannot_cross_company(self):
        other_company = self.env["res.company"].create(
            {"name": "Marketing Other Company %s" % uuid.uuid4()}
        )
        other_source = (
            self.env["marketing.center.source"]
            .sudo()
            .create(
                {
                    "name": "Other company source",
                    "company_id": other_company.id,
                    "service": "manual.import",
                    "external_account_ref": "account-visible",
                    "currency_id": other_company.currency_id.id,
                    "timezone": "UTC",
                    "state": "active",
                }
            )
        )
        with self.assertRaises(ValidationError):
            self.env["marketing.center.team.source"].sudo().create(
                {
                    "team_id": self.team.id,
                    "source_id": other_source.id,
                    "access_mode": "read",
                }
            )

    def test_inactive_membership_cannot_borrow_another_active_member(self):
        membership = self.team.member_ids.filtered(
            lambda item: item.user_id == self.viewer
        )
        other_user = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Other active roster user",
                    "login": "marketing-other-%s" % uuid.uuid4(),
                    "company_id": self.env.company.id,
                    "company_ids": [Command.set(self.env.company.ids)],
                    "groups_id": [Command.set(self.viewer_group.ids)],
                }
            )
        )
        self.env["marketing.center.team.member"].create(
            {
                "team_id": self.team.id,
                "user_id": other_user.id,
                "role": "viewer",
            }
        )
        membership.write({"active": False})
        self.assertFalse(
            self.env["marketing.center.source"]
            .with_user(self.viewer)
            .search([("id", "=", self.visible_source.id)])
        )

    def test_capability_is_the_intersection_of_group_role_and_source_mode(self):
        analyst = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Marketing roster analyst",
                    "login": "marketing-analyst-%s" % uuid.uuid4(),
                    "company_id": self.env.company.id,
                    "company_ids": [Command.set(self.env.company.ids)],
                    "groups_id": [Command.set(self.analyst_group.ids)],
                }
            )
        )
        membership = self.env["marketing.center.team.member"].create(
            {
                "team_id": self.team.id,
                "user_id": analyst.id,
                "role": "manager",
            }
        )
        source_link = self.team.source_link_ids.filtered(
            lambda link: link.source_id == self.visible_source
        )

        self.assertTrue(self.visible_source._has_user_capability("read", analyst))
        self.assertFalse(
            self.visible_source._has_user_capability("prepare", analyst),
            "The source mode must cap a more privileged team role.",
        )

        source_link.write({"access_mode": "approve"})
        self.assertTrue(self.visible_source._has_user_capability("prepare", analyst))
        self.assertFalse(
            self.visible_source._has_user_capability("operate", analyst),
            "The global Analyst group must cap a Manager team role.",
        )

        membership.write({"role": "viewer"})
        self.assertTrue(self.visible_source._has_user_capability("read", analyst))
        self.assertFalse(
            self.visible_source._has_user_capability("prepare", analyst),
            "The team role must cap the user's global group.",
        )

    def test_inactive_scope_components_revoke_the_stored_projection(self):
        membership = self.team.member_ids.filtered(
            lambda item: item.user_id == self.viewer
        )
        source_link = self.team.source_link_ids.filtered(
            lambda link: link.source_id == self.visible_source
        )
        self.assertIn(self.viewer, self.visible_source.sudo().access_user_ids)

        source_link.write({"active": False})
        self.assertNotIn(self.viewer, self.visible_source.sudo().access_user_ids)
        source_link.write({"active": True})

        self.team.write({"active": False})
        self.assertNotIn(self.viewer, self.visible_source.sudo().access_user_ids)
        self.team.write({"active": True})

        membership.write({"active": False})
        self.assertNotIn(self.viewer, self.visible_source.sudo().access_user_ids)
        membership.write({"active": True})

        self.visible_source.write({"active": False})
        self.assertNotIn(self.viewer, self.visible_source.sudo().access_user_ids)
        self.assertFalse(
            self.env["marketing.center.source"]
            .with_user(self.viewer)
            .with_context(active_test=False)
            .search([("id", "=", self.visible_source.id)])
        )

    def test_administrator_bypasses_roster_but_not_active_company_scope(self):
        self.assertTrue(
            self.hidden_source._has_user_capability(
                "approve", user=self.marketing_admin
            )
        )
        other_company = self.env["res.company"].create(
            {"name": "Capability boundary %s" % uuid.uuid4()}
        )
        other_source = (
            self.env["marketing.center.source"]
            .sudo()
            .create(
                {
                    "name": "Other-company capability source",
                    "company_id": other_company.id,
                    "service": "manual.import",
                    "external_account_ref": "other-company-capability",
                    "currency_id": other_company.currency_id.id,
                    "timezone": "UTC",
                    "state": "active",
                }
            )
        )
        self.assertFalse(
            other_source._has_user_capability("read", user=self.marketing_admin)
        )

    def test_scope_parent_identities_are_immutable(self):
        other_company = self.env["res.company"].create(
            {"name": "Immutable scope company %s" % uuid.uuid4()}
        )
        with self.assertRaises(AccessError):
            self.team.write({"company_id": other_company.id})
        with self.assertRaises(AccessError):
            self.visible_source.write({"company_id": other_company.id})
        membership = self.team.member_ids.filtered(
            lambda item: item.user_id == self.viewer
        )
        with self.assertRaises(AccessError):
            membership.write({"team_id": False})

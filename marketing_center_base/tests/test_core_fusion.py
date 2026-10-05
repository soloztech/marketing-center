import importlib

from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import SavepointCase

from ..core_migration import (
    CORE,
    LEGACY_MODULES,
    adopt_legacy_records,
    ensure_legacy_aliases,
    legacy_catalog,
    require_fused_core,
)


@tagged("-at_install", "post_install")
class TestMarketingCoreFusion(SavepointCase):
    def test_legacy_python_paths_share_implementation_and_capabilities(self):
        for name in ("crm_lead", "service", "native_utm", "tokens", "campaign_board"):
            self.assertIs(
                importlib.import_module(
                    "odoo.addons.marketing_center_crm.models." + name
                ),
                importlib.import_module(
                    "odoo.addons.marketing_center_base.models.crm." + name
                ),
            )
        self.assertIs(
            importlib.import_module(
                "odoo.addons.marketing_center_dashboard.models.dashboard"
            ),
            importlib.import_module(
                "odoo.addons.marketing_center_base.models.dashboard"
            ),
        )

    def test_active_legacy_aliases_preserve_all_canonical_targets(self):
        active = (
            self.env["ir.module.module"]
            .search([("name", "in", list(LEGACY_MODULES)), ("state", "=", "installed")])
            .mapped("name")
        )
        ensure_legacy_aliases(self.env)
        for module in active:
            for name in legacy_catalog()[module]:
                self.assertEqual(
                    self.env.ref(module + "." + name), self.env.ref(CORE + "." + name)
                )
        before = self.env["ir.model.data"].search_count(
            [("module", "in", list(LEGACY_MODULES))]
        )
        ensure_legacy_aliases(self.env)
        self.assertEqual(
            self.env["ir.model.data"].search_count(
                [("module", "in", list(LEGACY_MODULES))]
            ),
            before,
        )

    def test_alias_collision_is_rejected_without_repointing(self):
        name = "action_marketing_leads"
        data = self.env["ir.model.data"].search(
            [("module", "=", "marketing_center_crm"), ("name", "=", name)]
        )
        other = self.env.ref(CORE + ".action_marketing_opportunities")
        if data:
            data.res_id = other.id
        else:
            data = self.env["ir.model.data"].create(
                {
                    "module": "marketing_center_crm",
                    "name": name,
                    "model": other._name,
                    "res_id": other.id,
                }
            )
        with self.assertRaisesRegex(ValidationError, "XML-id collision"):
            ensure_legacy_aliases(self.env, modules=("marketing_center_crm",))
        self.assertEqual(data.res_id, other.id)

    def test_unknown_legacy_metadata_is_rejected_before_adoption(self):
        record = self.env.ref(CORE + ".action_marketing_leads")
        data = self.env["ir.model.data"].create(
            {
                "module": "marketing_center_crm",
                "name": "unknown_fusion_probe",
                "model": record._name,
                "res_id": record.id,
            }
        )
        with self.assertRaisesRegex(ValidationError, "Undeclared legacy"):
            adopt_legacy_records(self.env)
        self.assertFalse(data.noupdate)
        self.assertFalse(
            self.env.ref(CORE + ".unknown_fusion_probe", raise_if_not_found=False)
        )

    def test_shim_guard_reads_stored_version_instead_of_disk_version(self):
        core = self.env["ir.module.module"].search([("name", "=", CORE)])
        core.latest_version = "16.0.1.4.0"
        with self.assertRaisesRegex(ValidationError, "Upgrade marketing_center_base"):
            require_fused_core(self.env)

    def test_overview_is_visible_at_root_for_a_viewer(self):
        overview = self.env.ref(CORE + ".menu_marketing_dashboard_overview")
        self.assertEqual(
            overview.parent_id, self.env.ref(CORE + ".menu_marketing_center_root")
        )
        self.assertEqual(overview.sequence, 5)
        viewer = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Core overview viewer",
                    "login": "core_fusion_viewer",
                    "groups_id": [
                        (
                            6,
                            0,
                            [self.env.ref(CORE + ".group_marketing_center_viewer").id],
                        )
                    ],
                }
            )
        )
        self.assertIn(
            overview.id, self.env["ir.ui.menu"].with_user(viewer)._visible_menu_ids()
        )

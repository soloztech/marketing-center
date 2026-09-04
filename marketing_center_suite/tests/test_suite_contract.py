from odoo.modules.module import get_manifest
from odoo.tests import tagged
from odoo.tests.common import SavepointCase

DIRECT_DEPENDENCIES = (
    "marketing_center_contact_center_crm",
    "marketing_center_dashboard",
    "marketing_center_google",
    "marketing_center_meta_crm",
    "marketing_center_sale_account",
    "marketing_center_website_crm",
)

COMPONENT_MODULES = {
    "marketing_center_account",
    "marketing_center_base",
    "marketing_center_contact_center",
    "marketing_center_contact_center_crm",
    "marketing_center_crm",
    "marketing_center_dashboard",
    "marketing_center_google",
    "marketing_center_meta",
    "marketing_center_meta_crm",
    "marketing_center_sale",
    "marketing_center_sale_account",
    "marketing_center_web_ingress",
    "marketing_center_website",
    "marketing_center_website_crm",
}

UPDATEABLE_SECURITY_RULES = {
    "marketing_center_crm": {
        "rule_marketing_attribution_crm_link_own",
        "rule_marketing_attribution_crm_link_all",
        "rule_marketing_attribution_crm_revocation_own",
        "rule_marketing_attribution_crm_revocation_all",
        "rule_marketing_attribution_crm_effective_link_own",
        "rule_marketing_attribution_crm_effective_link_all",
        "rule_marketing_business_event_crm_link_own",
        "rule_marketing_business_event_crm_link_all",
        "rule_marketing_attribution_crm_link_system",
        "rule_marketing_attribution_crm_revocation_system",
        "rule_marketing_attribution_crm_effective_link_system",
        "rule_marketing_business_event_crm_link_system",
        "rule_marketing_crm_lead_equivalence_own",
        "rule_marketing_crm_lead_equivalence_all",
        "rule_marketing_crm_lead_equivalence_system",
    },
    "marketing_center_sale": {
        "rule_marketing_sale_order_crm_link_own",
        "rule_marketing_sale_order_crm_link_all",
        "rule_marketing_event_sale_link_own",
        "rule_marketing_event_sale_link_all",
        "rule_marketing_sale_order_crm_link_system",
        "rule_marketing_event_sale_link_system",
    },
    "marketing_center_account": {
        "rule_marketing_event_account_move_link_account",
        "rule_marketing_event_account_payment_link_account",
        "rule_marketing_event_account_move_link_analyst",
        "rule_marketing_event_account_payment_link_analyst",
        "rule_marketing_event_account_move_link_system",
        "rule_marketing_event_account_payment_link_system",
    },
    "marketing_center_sale_account": {
        "rule_marketing_move_sale_link_billing",
        "rule_marketing_move_sale_link_sales_own",
        "rule_marketing_move_sale_link_sales_all",
        "rule_marketing_move_sale_link_analyst",
        "rule_marketing_move_sale_link_system",
        "rule_marketing_sale_account_projection_billing",
        "rule_marketing_sale_account_projection_analyst",
        "rule_marketing_sale_account_projection_system",
    },
}


@tagged("-at_install", "post_install")
class TestMarketingCenterSuiteContract(SavepointCase):
    def test_manifest_and_dependency_closure_are_exact(self):
        manifest = get_manifest("marketing_center_suite")
        self.assertEqual(tuple(manifest["depends"]), DIRECT_DEPENDENCIES)
        self.assertTrue(manifest["installable"])
        self.assertTrue(manifest["application"])
        self.assertFalse(manifest["auto_install"])
        self.assertEqual(manifest["data"], [])

        pending = list(DIRECT_DEPENDENCIES)
        closure = set()
        while pending:
            module = pending.pop()
            if module in closure:
                continue
            closure.add(module)
            pending.extend(
                dependency
                for dependency in get_manifest(module).get("depends", ())
                if dependency.startswith("marketing_center_")
            )
        self.assertEqual(closure, COMPONENT_MODULES)

    def test_complete_component_closure_is_installed(self):
        modules = (
            self.env["ir.module.module"]
            .sudo()
            .search([("name", "in", sorted(COMPONENT_MODULES))])
        )
        self.assertEqual(set(modules.mapped("name")), COMPONENT_MODULES)
        self.assertEqual(set(modules.mapped("state")), {"installed"})

    def test_suite_is_the_only_application_in_the_distribution(self):
        module_names = COMPONENT_MODULES | {"marketing_center_suite"}
        modules = (
            self.env["ir.module.module"]
            .sudo()
            .search([("name", "in", sorted(module_names))])
        )
        self.assertEqual(
            set(modules.filtered("application").mapped("name")),
            {"marketing_center_suite"},
        )

    def test_suite_owns_no_menu_and_keeps_one_canonical_root(self):
        model_data = self.env["ir.model.data"].sudo()
        self.assertFalse(
            model_data.search_count(
                [
                    ("module", "=", "marketing_center_suite"),
                    ("model", "=", "ir.ui.menu"),
                ]
            )
        )

        menu_data = model_data.search(
            [
                ("module", "in", sorted(COMPONENT_MODULES)),
                ("model", "=", "ir.ui.menu"),
            ]
        )
        menus = (
            self.env["ir.ui.menu"].sudo().browse(menu_data.mapped("res_id")).exists()
        )
        roots = menus.filtered(lambda menu: not menu.parent_id)
        self.assertEqual(
            roots, self.env.ref("marketing_center_base.menu_marketing_center_root")
        )

    def test_security_rules_remain_updateable_by_addon_upgrades(self):
        model_data = self.env["ir.model.data"].sudo()
        for module, names in UPDATEABLE_SECURITY_RULES.items():
            rules = model_data.search(
                [
                    ("module", "=", module),
                    ("name", "in", sorted(names)),
                    ("model", "=", "ir.rule"),
                ]
            )
            self.assertEqual(set(rules.mapped("name")), names)
            self.assertFalse(rules.filtered("noupdate"))

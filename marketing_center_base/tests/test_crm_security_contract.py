from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import SavepointCase

from ..core_migration import CORE, legacy_catalog


@tagged("-at_install", "post_install")
class TestMarketingCenterCrmSecurityContract(SavepointCase):
    def test_security_rules_remain_updateable_by_addon_upgrades(self):
        model_data = self.env["ir.model.data"].sudo()
        names = {
            name
            for entries in legacy_catalog().values()
            for name, model in entries.items()
            if model == "ir.rule"
        }
        self.assertEqual(len(names), 21)
        rules = model_data.search(
            [
                ("module", "=", CORE),
                ("name", "in", sorted(names)),
                ("model", "=", "ir.rule"),
            ]
        )
        self.assertEqual(set(rules.mapped("name")), names)
        self.assertFalse(rules.filtered("noupdate"))

    def test_system_admin_can_create_company_with_inactive_form_defaults(self):
        # Model the native System role independently of optional applications.
        # This group fixture is rolled back by the native transaction case.
        optional = [
            self.env.ref(xmlid, raise_if_not_found=False)
            for xmlid in (
                "contact_center_base.group_contact_center_admin",
                "marketing_center_base.group_marketing_center_admin",
            )
        ]
        self.env.ref("base.group_system").write(
            {
                "implied_ids": [
                    Command.unlink(group.id) for group in optional if group
                ],
            }
        )
        user = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Company creation operator",
                    "login": "company-form-default-operator",
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [
                        (
                            6,
                            0,
                            (
                                self.env.ref("base.group_system")
                                | self.env.ref("base.group_user")
                                | self.env.ref("base.group_partner_manager")
                            ).ids,
                        )
                    ],
                }
            )
        )
        for group in optional:
            if group:
                self.assertFalse(user.has_group(group.get_external_id()[group.id]))
        company = (
            self.env["res.company"]
            .with_user(user)
            .create(
                {
                    "name": "Standard inactive policy company",
                    "crm_cross_source_dedup_enabled": False,
                    "crm_cross_source_reviewer_id": False,
                    "crm_cross_source_window_hours": 24,
                }
            )
        )
        self.assertTrue(company.exists())
        self.assertFalse(company.crm_cross_source_dedup_enabled)
        with self.assertRaises(AccessError):
            self.env["res.company"].with_user(user).create(
                {
                    "name": "Unauthorized nondefault policy",
                    "crm_cross_source_window_hours": 48,
                }
            )

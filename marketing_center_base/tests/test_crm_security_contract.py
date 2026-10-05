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

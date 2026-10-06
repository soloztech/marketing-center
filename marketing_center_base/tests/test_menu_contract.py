from odoo.tests import tagged
from odoo.tests.common import SavepointCase


@tagged("-at_install", "post_install")
class TestMarketingCenterMenuContract(SavepointCase):
    def test_installed_analytics_keep_one_canonical_root(self):
        modules = (
            self.env["ir.module.module"]
            .sudo()
            .search([("state", "=", "installed")])
            .filtered(lambda module: module.name.startswith("marketing_center_"))
        )
        menu_data = (
            self.env["ir.model.data"]
            .sudo()
            .search(
                [
                    ("module", "in", modules.mapped("name")),
                    ("model", "=", "ir.ui.menu"),
                ]
            )
        )
        menus = (
            self.env["ir.ui.menu"].sudo().browse(menu_data.mapped("res_id")).exists()
        )
        roots = menus.filtered(lambda menu: not menu.parent_id)
        self.assertEqual(
            roots, self.env.ref("marketing_center_base.menu_marketing_center_root")
        )

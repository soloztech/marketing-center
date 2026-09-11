from odoo import models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def action_open_content_catalog(self):
        self.ensure_one()
        self.check_access_rights("read")
        self.check_access_rule("read")
        return self.env["marketing.center.catalog.item"].action_open_catalog(
            company_id=self.company_id.id,
            product_ids=self.order_line.product_id.ids,
        )

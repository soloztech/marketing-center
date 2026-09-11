from odoo import models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    def action_open_content_catalog(self):
        self.ensure_one()
        self.check_access_rights("read")
        self.check_access_rule("read")
        return self.env["marketing.center.catalog.item"].action_open_catalog(
            company_id=(self.company_id or self.env.company).id,
            product_ids=self.with_context(active_test=False).product_variant_ids.ids,
        )


class ProductProduct(models.Model):
    _inherit = "product.product"

    def action_open_content_catalog(self):
        self.ensure_one()
        self.check_access_rights("read")
        self.check_access_rule("read")
        return self.env["marketing.center.catalog.item"].action_open_catalog(
            company_id=(self.company_id or self.env.company).id,
            product_ids=self.ids,
        )

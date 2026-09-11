from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user


class TestSaleCatalog(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.other_company = cls.env["res.company"].create({"name": "Catalog Other"})
        cls.reader = new_test_user(
            cls.env,
            login="catalog_sales_reader",
            groups=(
                "sales_team.group_sale_salesman,"
                "marketing_center_catalog.group_catalog_reader"
            ),
            company_id=cls.company.id,
            company_ids=[(6, 0, cls.company.ids)],
        )
        cls.salesman = new_test_user(
            cls.env,
            login="catalog_sales_without_catalog",
            groups="sales_team.group_sale_salesman",
            company_id=cls.company.id,
            company_ids=[(6, 0, cls.company.ids)],
        )
        cls.partner = cls.env["res.partner"].create({"name": "Catalog Customer"})
        cls.products = cls.env["product.product"].create(
            [
                {"name": "Catalog Product A", "type": "service"},
                {"name": "Catalog Product B", "type": "service"},
            ]
        )
        cls.order = cls.env["sale.order"].create(
            {
                "partner_id": cls.partner.id,
                "company_id": cls.company.id,
                "user_id": cls.reader.id,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": product.id,
                            "product_uom_qty": 1,
                            "price_unit": 10,
                        },
                    )
                    for product in cls.products
                ],
            }
        )

    def test_order_opens_removable_product_filter_without_changing_quote(self):
        before = self.order.read(["state", "order_line", "amount_total"])
        action = self.order.with_user(self.reader).action_open_content_catalog()
        self.assertEqual(action["type"], "ir.actions.client")
        self.assertEqual(action["tag"], "marketing_center_catalog.browser")
        self.assertEqual(action["params"]["company_id"], self.company.id)
        self.assertEqual(set(action["params"]["product_ids"]), set(self.products.ids))
        self.assertNotIn(
            "domain", action, "Product selection is a removable browser filter"
        )
        self.assertEqual(
            before, self.order.read(["state", "order_line", "amount_total"])
        )

    def test_empty_quotation_opens_full_company_library(self):
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "company_id": self.company.id,
                "user_id": self.reader.id,
            }
        )
        action = order.with_user(self.reader).action_open_content_catalog()
        self.assertEqual(action["params"]["company_id"], self.company.id)
        self.assertFalse(action["params"]["product_ids"])

    def test_catalog_permission_is_required_on_rpc(self):
        self.order.user_id = self.salesman
        with self.assertRaises(AccessError):
            self.order.with_user(self.salesman).action_open_content_catalog()

    def test_order_company_access_is_checked_on_rpc(self):
        other_order = (
            self.env["sale.order"]
            .with_company(self.other_company)
            .create(
                {
                    "partner_id": self.partner.id,
                    "company_id": self.other_company.id,
                    "user_id": False,
                }
            )
        )
        with self.assertRaises(AccessError):
            other_order.with_user(self.reader).with_context(
                allowed_company_ids=self.company.ids
            ).action_open_content_catalog()

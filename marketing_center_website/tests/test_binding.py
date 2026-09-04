from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import SavepointCase
from odoo.tools import mute_logger


class TestMarketingWebsiteIngressBinding(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.website = cls.env.ref("website.default_website")
        cls.endpoint = cls.env["marketing.web.ingress.endpoint"].create(
            {
                "name": "Website binding endpoint",
                "company_id": cls.website.company_id.id,
                "allowed_origins": "https://www.example.test",
                "allowed_hosts": "www.example.test",
            }
        )
        cls.binding = cls.env["marketing.website.ingress.binding"].search(
            [("website_id", "=", cls.website.id)], limit=1
        )
        if cls.binding:
            cls.binding.write({"endpoint_id": cls.endpoint.id, "active": True})
        else:
            cls.binding = cls.env["marketing.website.ingress.binding"].create(
                {
                    "website_id": cls.website.id,
                    "endpoint_id": cls.endpoint.id,
                }
            )

    def test_one_active_same_company_endpoint_per_website(self):
        self.assertTrue(self.binding.active)
        self.assertEqual(self.binding.company_id, self.website.company_id)
        with mute_logger("odoo.sql_db"), self.assertRaises(IntegrityError):
            with self.env.cr.savepoint(flush=False):
                self.env["marketing.website.ingress.binding"].create(
                    {
                        "website_id": self.website.id,
                        "endpoint_id": self.endpoint.id,
                    }
                )

        other_company = self.env["res.company"].create({"name": "Website Company B"})
        other_endpoint = (
            self.env["marketing.web.ingress.endpoint"]
            .sudo()
            .with_company(other_company)
            .create(
                {
                    "name": "Other company endpoint",
                    "company_id": other_company.id,
                    "allowed_origins": "https://other.example.test",
                    "allowed_hosts": "other.example.test",
                }
            )
        )
        with self.assertRaises(UserError):
            self.binding.write({"endpoint_id": other_endpoint.id})

    def test_active_binding_fences_endpoint_and_website_company(self):
        with self.assertRaises(UserError):
            self.endpoint.write({"active": False})
        other_company = self.env["res.company"].create({"name": "Move target"})
        with self.assertRaises(UserError):
            self.website.write({"company_id": other_company.id})

        self.binding.write({"active": False})
        self.endpoint.write({"active": False})
        with self.assertRaises(UserError):
            self.binding.write({"active": True})

    def test_binding_is_archived_not_deleted(self):
        with self.assertRaises(AccessError):
            self.binding.unlink()

    def test_acl_and_company_rule_protect_configuration(self):
        viewer_group = self.env.ref(
            "marketing_center_base.group_marketing_center_viewer"
        )
        viewer = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Website ingress viewer",
                    "login": "website-ingress-viewer",
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, [self.env.company.id])],
                    "groups_id": [(6, 0, [viewer_group.id])],
                }
            )
        )
        with self.assertRaises(AccessError):
            self.binding.with_user(viewer).read(["website_id"])

        second_company = self.env["res.company"].create({"name": "Rule Company B"})
        second_website = (
            self.env["website"]
            .sudo()
            .with_company(second_company)
            .create({"name": "Company B website", "company_id": second_company.id})
        )
        second_endpoint = (
            self.endpoint.sudo()
            .with_company(second_company)
            .create(
                {
                    "name": "Rule endpoint B",
                    "company_id": second_company.id,
                    "allowed_origins": "https://b.example.test",
                    "allowed_hosts": "b.example.test",
                }
            )
        )
        second_binding = (
            self.env["marketing.website.ingress.binding"]
            .sudo()
            .with_company(second_company)
            .create(
                {
                    "website_id": second_website.id,
                    "endpoint_id": second_endpoint.id,
                }
            )
        )
        admin_group = self.env.ref("marketing_center_base.group_marketing_center_admin")
        restricted_admin = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Restricted marketing administrator",
                    "login": "restricted-website-ingress-admin",
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, [self.env.company.id])],
                    "groups_id": [(6, 0, [admin_group.id])],
                }
            )
        )
        self.assertFalse(
            self.env["marketing.website.ingress.binding"]
            .with_user(restricted_admin)
            .search_count([("id", "=", second_binding.id)])
        )

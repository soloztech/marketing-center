from lxml import etree

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import new_test_user

from odoo.addons.meta_webhook_base.tests.common import MetaWebhookCase


@tagged("post_install", "-at_install")
class TestMarketingWebhookAccess(MetaWebhookCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.marketing_admin = new_test_user(
            cls.env(context=dict(cls.env.context, no_reset_password=True)),
            login="marketing-webhook-admin",
            groups="marketing_center_base.group_marketing_center_admin",
            company_id=cls.env.company.id,
            company_ids=[Command.set(cls.env.company.ids)],
        )
        cls.system_user = new_test_user(
            cls.env(context=dict(cls.env.context, no_reset_password=True)),
            login="marketing-webhook-system",
            groups="base.group_system",
            company_id=cls.env.company.id,
            company_ids=[Command.set(cls.env.company.ids)],
        )

    def test_marketing_admin_reads_and_opens_endpoint_without_secret_url(self):
        endpoint = self.endpoint.with_user(self.marketing_admin)
        self.assertFalse(self.marketing_admin.has_group("base.group_system"))
        self.assertTrue(endpoint.check_access_rights("read"))
        values = endpoint.read(["name", "app_id", "revision", "subscription_state"])[0]
        self.assertEqual(values["name"], self.endpoint.name)
        for name in ("webhook_url", "routing_key", "verify_token_ref"):
            self.assertNotIn(name, values)
            self.assertNotIn(name, endpoint.fields_get())
            with self.assertRaises(AccessError):
                endpoint.read([name])

        view_id = self.env.ref("meta_webhook_base.view_meta_webhook_endpoint_form").id
        view = endpoint.get_view(view_id=view_id, view_type="form")
        arch = etree.fromstring(view["arch"].encode())
        self.assertFalse(arch.xpath("//field[@name='webhook_url']"))
        # Read the fields requested when the web client opens this form. A
        # generic read() also requests delivery_ids, whose separate ledger ACL
        # intentionally requires System and which is absent from this view.
        visible_fields = arch.xpath("//field[not(ancestor::field)]/@name")
        self.assertEqual(endpoint.read(visible_fields)[0]["name"], self.endpoint.name)

    def test_system_user_can_read_and_display_the_computed_url(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "web.base.url", "https://webhook-access.invalid/"
        )
        endpoint = self.endpoint.with_user(self.system_user)
        endpoint.invalidate_recordset(["webhook_url"])
        self.assertEqual(
            endpoint.read(["webhook_url"])[0]["webhook_url"],
            "https://webhook-access.invalid/meta/webhook/%s"
            % self.endpoint.routing_key,
        )
        view = endpoint.get_view(
            view_id=self.env.ref(
                "meta_webhook_base.view_meta_webhook_endpoint_form"
            ).id,
            view_type="form",
        )
        arch = etree.fromstring(view["arch"].encode())
        self.assertTrue(arch.xpath("//field[@name='webhook_url']"))

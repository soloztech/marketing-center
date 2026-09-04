import decimal
import uuid

from odoo import Command, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.marketing_center_account.models.tokens import (
    MARKETING_ACCOUNT_LINK_WRITE_TOKEN,
    MARKETING_ACCOUNT_TRANSITION_GUARD,
)
from odoo.addons.marketing_center_base.services import MarketingBusinessEventDTO


class TestMarketingCenterSaleAccount(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suffix = uuid.uuid4().hex[:8]
        cls.receivable = cls.env["account.account"].create(
            {
                "name": "Bridge Receivable %s" % suffix,
                "code": "BR%s" % suffix[:6].upper(),
                "account_type": "asset_receivable",
                "reconcile": True,
                "company_id": cls.env.company.id,
            }
        )
        cls.income = cls.env["account.account"].create(
            {
                "name": "Bridge Income %s" % suffix,
                "code": "BI%s" % suffix[:6].upper(),
                "account_type": "income",
                "company_id": cls.env.company.id,
            }
        )
        cls.sales_journal = cls.env["account.journal"].create(
            {
                "name": "Bridge Sales %s" % suffix,
                "code": "BS%s" % suffix[:3].upper(),
                "type": "sale",
                "company_id": cls.env.company.id,
            }
        )
        cls.partner = cls.env["res.partner"].create(
            {"name": "Bridge Partner %s" % suffix}
        )
        cls.partner.with_company(
            cls.env.company
        ).property_account_receivable_id = cls.receivable
        cls.product = cls.env["product.product"].create(
            {
                "name": "Bridge Product %s" % suffix,
                "type": "service",
                "invoice_policy": "order",
                "list_price": 100,
                "property_account_income_id": cls.income.id,
            }
        )

    def _order(self, amount=100, lead=None):
        values = {
            "partner_id": self.partner.id,
            "company_id": self.env.company.id,
            "order_line": [
                Command.create(
                    {
                        "product_id": self.product.id,
                        "product_uom_qty": 1,
                        "price_unit": amount,
                    }
                )
            ],
        }
        if lead:
            values["opportunity_id"] = lead.id
        order = self.env["sale.order"].create(values)
        order.action_confirm()
        return order

    def _manual_invoice(self, amount=100, **values):
        invoice_values = {
            "move_type": "out_invoice",
            "company_id": self.env.company.id,
            "journal_id": self.sales_journal.id,
            "partner_id": self.partner.id,
            "invoice_date": fields.Date.today(),
            "invoice_line_ids": [
                Command.create(
                    {
                        "name": values.pop("line_name", "Bridge revenue"),
                        "account_id": self.income.id,
                        "quantity": 1,
                        "price_unit": amount,
                    }
                )
            ],
        }
        invoice_values.update(values)
        return self.env["account.move"].create(invoice_values)

    def _invoice_event(self, invoice):
        return invoice.marketing_account_event_link_ids.filtered(
            lambda link: link.role == "source"
        ).mapped("event_id")

    def _account_link(self, event, invoice):
        return event.account_move_link_ids.filtered(
            lambda link: link.move_id == invoice and link.role == "source"
        )

    def _create_legacy_event_link(self, invoice):
        occurrence = "legacy:%s" % invoice.id
        dto = MarketingBusinessEventDTO(
            event_class="revenue",
            event_type="invoice_posted",
            source_system="odoo.account",
            source_model="account.move",
            source_res_id=invoice.id,
            source_occurrence_ref=occurrence,
            source_evidence_ref="legacy-account-link:%s" % invoice.id,
            business_event_key="legacy.account.move:%s" % invoice.id,
            occurred_at=fields.Datetime.now(),
            evidence_level="imported",
            amount_signed=decimal.Decimal(str(invoice.amount_untaxed)),
            currency=invoice.currency_id.name,
            extensions={"account.amount_basis": "untaxed"},
        )
        result = self.env["marketing.business.event.service"]._ingest_event(
            self.env.company, dto
        )
        event = self.env["marketing.business.event"].browse(result.event_id)
        account_link = (
            self.env["marketing.business.event.account.move.link"]
            .sudo()
            .with_context(
                marketing_account_link_write_token=MARKETING_ACCOUNT_LINK_WRITE_TOKEN
            )
            .create(
                {
                    "company_id": self.env.company.id,
                    "event_id": event.id,
                    "move_id": invoice.id,
                    "role": "source",
                }
            )
        )
        return event, account_link

    def test_typed_invoice_line_projects_order_and_crm(self):
        lead = self.env["crm.lead"].create(
            {"name": "Bridge causal lead", "company_id": self.env.company.id}
        )
        order = self._order(80, lead=lead)
        invoice = order._create_invoices()
        invoice.action_post()
        event = self._invoice_event(invoice)

        self.assertEqual(len(event), 1)
        self.assertEqual(invoice.marketing_sale_order_count, 1)
        self.assertEqual(invoice.marketing_account_sale_link_ids.order_id, order)
        self.assertEqual(event.sale_order_link_ids.order_id, order)
        self.assertEqual(event.crm_link_ids.lead_id, lead)
        self.assertEqual(
            invoice.marketing_account_sale_link_ids.source_ref,
            "account.move.invoice_line_ids.sale_line_ids",
        )

    def test_names_origins_references_and_partner_never_infer_an_order(self):
        order = self._order(25)
        invoice = self._manual_invoice(
            25,
            invoice_origin=order.name,
            ref=order.name,
            line_name=order.name,
        )
        invoice.action_post()
        event = self._invoice_event(invoice)
        account_link = self._account_link(event, invoice)
        projection = self.env["marketing.sale.account.projection"].search(
            [("account_link_id", "=", account_link.id)]
        )

        self.assertFalse(invoice.invoice_line_ids.sale_line_ids)
        self.assertFalse(invoice.marketing_account_sale_link_ids)
        self.assertFalse(event.sale_order_link_ids)
        self.assertEqual(projection.source_order_count, 0)

    def test_empty_projection_is_frozen_but_repost_gets_a_new_snapshot(self):
        order = self._order(40)
        invoice = self._manual_invoice(40, invoice_origin=order.name)
        invoice.action_post()
        first_event = self._invoice_event(invoice)
        account_link = self._account_link(first_event, invoice)
        first_projection = self.env["marketing.sale.account.projection"].search(
            [("account_link_id", "=", account_link.id)]
        )
        self.assertEqual(first_projection.source_order_count, 0)

        invoice.button_draft()
        invoice.invoice_line_ids.write(
            {"sale_line_ids": [Command.set(order.order_line.ids)]}
        )
        self.env["marketing.account.service"]._link_event_move(
            first_event, invoice, "source"
        )
        self.assertFalse(first_event.sale_order_link_ids)
        self.assertFalse(invoice.marketing_account_sale_link_ids)

        invoice.action_post()
        events = self._invoice_event(invoice).sorted("id")
        self.assertEqual(len(events), 2)
        self.assertFalse(events[0].sale_order_link_ids)
        self.assertEqual(events[1].sale_order_link_ids.order_id, order)

    def test_repost_freezes_each_occurrence_typed_causality(self):
        first_order = self._order(30)
        second_order = self._order(30)
        invoice = first_order._create_invoices()
        invoice.action_post()
        first_event = self._invoice_event(invoice)
        self.assertEqual(first_event.sale_order_link_ids.order_id, first_order)

        invoice.button_draft()
        invoice.invoice_line_ids.write(
            {"sale_line_ids": [Command.set(second_order.order_line.ids)]}
        )
        invoice.action_post()
        events = self._invoice_event(invoice).sorted("id")

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].sale_order_link_ids.order_id, first_order)
        self.assertEqual(events[1].sale_order_link_ids.order_id, second_order)
        self.assertEqual(
            set(invoice.marketing_account_sale_link_ids.order_id.ids),
            {first_order.id, second_order.id},
        )

    def test_multi_order_invoice_keeps_one_unsplit_business_event_amount(self):
        first_order = self._order(30)
        second_order = self._order(40)
        invoice = self.env["account.move"].create(
            {
                "move_type": "out_invoice",
                "company_id": self.env.company.id,
                "journal_id": self.sales_journal.id,
                "partner_id": self.partner.id,
                "invoice_date": fields.Date.today(),
                "invoice_line_ids": [
                    Command.create(
                        {
                            "name": "First typed order",
                            "account_id": self.income.id,
                            "quantity": 1,
                            "price_unit": 30,
                            "sale_line_ids": [Command.set(first_order.order_line.ids)],
                        }
                    ),
                    Command.create(
                        {
                            "name": "Second typed order",
                            "account_id": self.income.id,
                            "quantity": 1,
                            "price_unit": 40,
                            "sale_line_ids": [Command.set(second_order.order_line.ids)],
                        }
                    ),
                ],
            }
        )
        invoice.action_post()
        event = self._invoice_event(invoice)

        self.assertEqual(len(event), 1)
        self.assertEqual(len(event.sale_order_link_ids), 2)
        self.assertEqual(
            set(event.sale_order_link_ids.order_id.ids),
            {first_order.id, second_order.id},
        )
        self.assertEqual(event.amount_signed_micros, 70_000_000)
        self.assertEqual(set(event.sale_order_link_ids.event_id.ids), {event.id})

    def test_late_install_backfill_is_bounded_and_idempotent(self):
        order = self._order(60)
        invoice = order._create_invoices()
        invoice.with_context(
            marketing_account_transition_guard=MARKETING_ACCOUNT_TRANSITION_GUARD
        ).action_post()
        self.assertFalse(self._invoice_event(invoice))
        event, account_link = self._create_legacy_event_link(invoice)
        self.assertFalse(event.sale_order_link_ids)

        service = self.env["marketing.account.service"]
        first = service._backfill_sale_account_projections(
            self.env.company, after_id=account_link.id - 1, limit=1
        )
        second = service._backfill_sale_account_projections(
            self.env.company, after_id=account_link.id - 1, limit=1
        )
        projection = self.env["marketing.sale.account.projection"].search(
            [("account_link_id", "=", account_link.id)]
        )

        self.assertEqual(
            first,
            {
                "processed": 1,
                "projected": 1,
                "skipped_missing_source": 0,
                "last_id": account_link.id,
                "has_more": False,
            },
        )
        self.assertEqual(
            second,
            {
                "processed": 1,
                "projected": 0,
                "skipped_missing_source": 0,
                "last_id": account_link.id,
                "has_more": False,
            },
        )
        self.assertEqual(projection.source_order_count, 1)
        self.assertEqual(event.sale_order_link_ids.order_id, order)
        self.assertEqual(invoice.marketing_account_sale_link_ids.order_id, order)
        with self.assertRaises(ValidationError):
            service._backfill_sale_account_projections(False)
        with self.assertRaises(ValidationError):
            service._backfill_sale_account_projections(self.env.company, after_id=True)
        with self.assertRaises(ValidationError):
            service._backfill_sale_account_projections(self.env.company, limit=0)

    def test_links_and_projection_receipts_are_immutable(self):
        order = self._order(15)
        invoice = order._create_invoices()
        invoice.action_post()
        event = self._invoice_event(invoice)
        account_link = self._account_link(event, invoice)
        link = invoice.marketing_account_sale_link_ids
        projection = self.env["marketing.sale.account.projection"].search(
            [("account_link_id", "=", account_link.id)]
        )

        self.assertTrue(account_link.marketing_sale_projection_claimed)
        with self.assertRaises(AccessError):
            link.sudo().write({"source_ref": "manual"})
        with self.assertRaises(AccessError):
            link.sudo().unlink()
        with self.assertRaises(AccessError):
            projection.sudo().write({"source_order_count": 0})
        with self.assertRaises(AccessError):
            projection.sudo().unlink()
        with self.assertRaises(AccessError):
            self.env["marketing.account.move.sale.link"].sudo().create(
                {
                    "company_id": self.env.company.id,
                    "move_id": invoice.id,
                    "order_id": order.id,
                    "source_ref": "manual",
                }
            )

    def test_deleted_invoice_and_order_leave_typed_projection_evidence(self):
        order = self._order(25)
        invoice = order._create_invoices()
        invoice.action_post()
        event = self._invoice_event(invoice)
        account_link = self._account_link(event, invoice)
        typed_link = invoice.marketing_account_sale_link_ids
        projection = self.env["marketing.sale.account.projection"].search(
            [("account_link_id", "=", account_link.id)]
        )
        move_id = invoice.id
        move_ref = invoice.name
        order_id = order.id
        order_ref = order.name

        invoice.button_draft()
        invoice.with_context(force_delete=True).unlink()
        typed_link.invalidate_recordset()
        account_link.invalidate_recordset()
        projection.invalidate_recordset(["move_id"])

        self.assertFalse(typed_link.move_id)
        self.assertTrue(typed_link.order_id)
        self.assertEqual(typed_link.move_model, "account.move")
        self.assertEqual(typed_link.move_res_id, move_id)
        self.assertEqual(typed_link.move_ref, move_ref)
        self.assertFalse(account_link.move_id)
        self.assertEqual(account_link.move_res_id, move_id)
        self.assertFalse(projection.move_id)
        move_action = order.action_view_marketing_account_moves()
        self.assertEqual(move_action["domain"], [("id", "in", [])])
        self.assertNotIn("res_id", move_action)

        order.with_context(disable_cancel_warning=True)._action_cancel()
        order.unlink()
        typed_link.invalidate_recordset()
        event.invalidate_recordset(["sale_order_count"])

        self.assertFalse(typed_link.order_id)
        self.assertEqual(typed_link.order_model, "sale.order")
        self.assertEqual(typed_link.order_res_id, order_id)
        self.assertEqual(typed_link.order_ref, order_ref)
        self.assertEqual(event.sale_order_count, 0)
        order_action = event.action_view_sale_orders()
        self.assertEqual(order_action["domain"], [("id", "in", [])])
        self.assertNotIn("res_id", order_action)

        result = self.env[
            "marketing.account.service"
        ]._backfill_sale_account_projections(
            self.env.company, after_id=account_link.id - 1, limit=1
        )
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["projected"], 0)
        self.assertEqual(result["skipped_missing_source"], 1)

    def test_fields_and_link_rules_do_not_leak_across_roles_or_companies(self):
        order = self._order(10)
        invoice = order._create_invoices()
        invoice.action_post()
        account_link = self._account_link(self._invoice_event(invoice), invoice)
        link = invoice.marketing_account_sale_link_ids
        projection = self.env["marketing.sale.account.projection"].search(
            [("account_link_id", "=", account_link.id)]
        )

        readonly = self.env.ref("account.group_account_readonly")
        readonly_user = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Bridge readonly accounting",
                    "login": "bridge-readonly-%s" % uuid.uuid4(),
                    "email": "bridge-readonly@example.invalid",
                    "company_id": self.env.company.id,
                    "company_ids": [Command.set(self.env.company.ids)],
                    "groups_id": [Command.set(readonly.ids)],
                }
            )
        )
        move_fields = self.env["account.move"].with_user(readonly_user).fields_get()
        self.assertNotIn("marketing_account_sale_link_ids", move_fields)
        self.assertNotIn("marketing_sale_order_count", move_fields)

        other_company = self.env["res.company"].create(
            {"name": "Bridge inaccessible company"}
        )
        analyst = self.env.ref("marketing_center_base.group_marketing_center_analyst")
        billing = self.env.ref("account.group_account_invoice")
        outsider = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Bridge company outsider",
                    "login": "bridge-outsider-%s" % uuid.uuid4(),
                    "email": "bridge-outsider@example.invalid",
                    "company_id": other_company.id,
                    "company_ids": [Command.set(other_company.ids)],
                    "groups_id": [Command.set((analyst | billing).ids)],
                }
            )
        )
        with self.assertRaises(AccessError):
            link.with_user(outsider).read(["id"])
        with self.assertRaises(AccessError):
            projection.with_user(outsider).read(["id"])

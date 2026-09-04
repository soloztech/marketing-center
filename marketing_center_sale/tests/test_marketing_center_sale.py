import decimal
import uuid

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.marketing_center_sale.models.tokens import (
    MARKETING_SALE_TRANSITION_GUARD,
)


class TestMarketingCenterSale(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suffix = str(uuid.uuid4())
        cls.partner = cls.env["res.partner"].create(
            {"name": "Sale Partner %s" % suffix}
        )
        cls.product = cls.env["product.product"].create(
            {
                "name": "Marketing Sale Product %s" % suffix,
                "type": "service",
                "list_price": 12.34,
            }
        )
        cls.lead = cls.env["crm.lead"].create(
            {
                "name": "Marketing Sale Lead %s" % suffix,
                "company_id": cls.env.company.id,
            }
        )
        cls.order = cls._create_order("Main", cls.lead)

    @classmethod
    def _create_order(cls, label, lead=None, company=None):
        company = company or cls.env.company
        return (
            cls.env["sale.order"]
            .with_company(company)
            .create(
                {
                    "partner_id": cls.partner.id,
                    "company_id": company.id,
                    "client_order_ref": label,
                    "opportunity_id": lead.id if lead else False,
                    "order_line": [
                        (
                            0,
                            0,
                            {
                                "product_id": cls.product.id,
                                "product_uom_qty": 2,
                                "price_unit": 12.34,
                            },
                        )
                    ],
                }
            )
        )

    def _events(self, order=None, event_type=None):
        order = order or self.order
        events = order.marketing_event_link_ids.mapped("event_id")
        return events.filtered(
            lambda event: not event_type or event.event_type == event_type
        )

    def test_first_proposal_confirmation_and_exact_cancellation(self):
        self.order.action_quotation_send()
        self.assertFalse(self._events(event_type="proposal_sent"))
        self.assertEqual(self.order.state, "draft")
        self.order.action_quotation_sent()
        proposal = self._events(event_type="proposal_sent")
        self.assertEqual(len(proposal), 1)
        self.assertFalse(proposal.has_amount)

        # A no-op write and a later draft -> sent cycle do not create a second
        # first-proposal event.
        self.order.write({"state": "sent"})
        self.order.action_draft()
        self.order.action_quotation_sent()
        self.assertEqual(len(self._events(event_type="proposal_sent")), 1)

        self.order.action_confirm()
        confirmation = self._events(event_type="order_confirmed")
        self.assertEqual(len(confirmation), 1)
        expected = decimal.Decimal(
            str(self.order.currency_id.round(self.order.amount_untaxed))
        )
        expected_micros = int(expected * decimal.Decimal(1_000_000))
        self.assertEqual(confirmation.amount_signed_micros, expected_micros)
        self.assertEqual(confirmation.currency_id, self.order.currency_id)

        # Confirming an already confirmed record is not another transition.
        self.order.action_confirm()
        self.assertEqual(len(self._events(event_type="order_confirmed")), 1)

        # The reversal uses the frozen confirmation amount even if another
        # customization later changes the order projection.
        self.order.order_line.write({"price_unit": 99})
        cancel_action = self.order.action_cancel()
        self.assertEqual(cancel_action["res_model"], "sale.order.cancel")
        self.assertEqual(self.order.state, "sale")
        self.assertFalse(self._events(event_type="order_cancelled"))
        self.order._action_cancel()
        cancellation = self._events(event_type="order_cancelled")
        self.assertEqual(len(cancellation), 1)
        self.assertEqual(cancellation.reverses_event_id, confirmation)
        self.assertEqual(cancellation.amount_signed_micros, -expected_micros)
        self.order.with_context(disable_cancel_warning=True)._action_cancel()
        self.assertEqual(len(self._events(event_type="order_cancelled")), 1)

    def test_free_order_emits_an_exact_zero_reversal_pair(self):
        order = self._create_order("Free")
        order.order_line.write({"price_unit": 0})
        order.action_confirm()
        confirmation = self._events(order, "order_confirmed")
        self.assertEqual(len(confirmation), 1)
        self.assertTrue(confirmation.has_amount)
        self.assertEqual(confirmation.amount_signed_micros, 0)
        self.assertEqual(confirmation.currency_id, order.currency_id)
        order.with_context(disable_cancel_warning=True)._action_cancel()
        cancellation = self._events(order, "order_cancelled")
        self.assertEqual(len(cancellation), 1)
        self.assertEqual(cancellation.amount_signed_micros, 0)
        self.assertEqual(cancellation.reverses_event_id, confirmation)

    def test_mail_send_transition_is_captured_without_tracking_row(self):
        order = self._create_order("Mail send")
        watermark = self.env["mail.tracking.value"].search_count(
            [
                ("mail_message_id.model", "=", "sale.order"),
                ("mail_message_id.res_id", "=", order.id),
                ("field.name", "=", "state"),
            ]
        )
        order.with_context(mark_so_as_sent=True).message_post(body="Quotation sent")
        self.assertEqual(order.state, "sent")
        self.assertEqual(len(self._events(order, "proposal_sent")), 1)
        self.assertEqual(
            self.env["mail.tracking.value"].search_count(
                [
                    ("mail_message_id.model", "=", "sale.order"),
                    ("mail_message_id.res_id", "=", order.id),
                    ("field.name", "=", "state"),
                ]
            ),
            watermark,
        )

    def test_reconfirmation_creates_a_new_monotonic_reversal_pair(self):
        self.order.action_confirm()
        self.order.with_context(disable_cancel_warning=True)._action_cancel()
        self.order.action_draft()
        self.order.action_confirm()
        self.order.with_context(disable_cancel_warning=True)._action_cancel()
        confirmations = self._events(event_type="order_confirmed")
        cancellations = self._events(event_type="order_cancelled")
        self.assertEqual(len(confirmations), 2)
        self.assertEqual(len(cancellations), 2)
        self.assertEqual(
            set(cancellations.mapped("reverses_event_id").ids), set(confirmations.ids)
        )
        sequences = [
            event.snapshot_json["extensions"]["sale.transition_sequence"]
            for event in confirmations | cancellations
        ]
        self.assertEqual(len(sequences), len(set(sequences)))

    def test_draft_cancellation_is_not_a_revenue_reversal(self):
        order = self._create_order("Draft cancellation")
        order.with_context(disable_cancel_warning=True)._action_cancel()
        self.assertFalse(self._events(order, "order_confirmed"))
        self.assertFalse(self._events(order, "order_cancelled"))

    def test_projection_failure_rolls_back_the_sale_transition(self):
        order = self._create_order("Invalid negative revenue")
        order.order_line.write({"price_unit": -1})
        with self.assertRaises(ValidationError):
            with self.env.cr.savepoint():
                order.action_confirm()
        order.invalidate_recordset(["state", "date_order"])
        self.assertEqual(order.state, "draft")
        self.assertFalse(self._events(order, "order_confirmed"))

    def test_order_lead_and_event_links_are_immutable_many_to_many(self):
        first_link = self.order.marketing_crm_link_ids
        self.assertEqual(first_link.lead_id, self.lead)
        other_lead = self.env["crm.lead"].create(
            {"name": "Second Sale Lead", "company_id": self.env.company.id}
        )
        self.order.write({"opportunity_id": other_lead.id})
        self.assertEqual(self.order.marketing_crm_lead_count, 2)
        self.order.action_confirm()
        event = self._events(event_type="order_confirmed")
        self.assertEqual(event.crm_lead_count, 2)
        self.assertEqual(event.sale_order_count, 1)
        with self.assertRaises(AccessError):
            first_link.sudo().write({"lead_id": other_lead.id})
        with self.assertRaises(AccessError):
            first_link.sudo().unlink()
        with self.assertRaises(AccessError):
            self.order.marketing_event_link_ids.sudo().unlink()

    def test_deleted_order_leaves_readable_evidence_without_dangling_actions(self):
        owner = self._create_salesperson(
            "deleted-order-owner", self.env.ref("sales_team.group_sale_salesman")
        )
        self.order.write({"user_id": owner.id})
        self.order.action_confirm()
        event = self._events(event_type="order_confirmed")
        crm_link = self.order.marketing_crm_link_ids[:1]
        event_link = self.order.marketing_event_link_ids.filtered(
            lambda link: link.event_id == event
        )
        order_id = self.order.id
        order_ref = self.order.name

        self.order.with_context(disable_cancel_warning=True)._action_cancel()
        self.order.unlink()
        crm_link.invalidate_recordset()
        event_link.invalidate_recordset()
        event.invalidate_recordset(["sale_order_count"])

        self.assertFalse(self.env["sale.order"].browse(order_id).exists())
        self.assertFalse(crm_link.order_id)
        self.assertEqual(crm_link.order_model, "sale.order")
        self.assertEqual(crm_link.order_res_id, order_id)
        self.assertEqual(crm_link.order_ref, order_ref)
        self.assertFalse(event_link.order_id)
        self.assertEqual(event_link.order_model, "sale.order")
        self.assertEqual(event_link.order_res_id, order_id)
        self.assertEqual(event_link.order_ref, order_ref)
        self.assertEqual(event.sale_order_count, 0)
        with self.assertRaises(AccessError):
            event_link.with_user(owner).read(["id"])

        for action in (
            self.lead.action_view_marketing_sale_orders(),
            event.action_view_sale_orders(),
        ):
            self.assertEqual(action["domain"], [("id", "in", [])])
            self.assertNotIn("res_id", action)

    def test_deleted_lead_leaves_sales_link_snapshot_without_dangling_action(self):
        lead = self.env["crm.lead"].create(
            {"name": "Disposable linked lead", "company_id": self.env.company.id}
        )
        order = self._create_order("Lead deletion", lead)
        link = order.marketing_crm_link_ids
        lead_id = lead.id
        lead_ref = lead.name

        lead.unlink()
        link.invalidate_recordset()
        order.invalidate_recordset(["marketing_crm_lead_count", "opportunity_id"])

        self.assertFalse(self.env["crm.lead"].browse(lead_id).exists())
        self.assertFalse(link.lead_id)
        self.assertEqual(link.lead_model, "crm.lead")
        self.assertEqual(link.lead_res_id, lead_id)
        self.assertEqual(link.lead_ref, lead_ref)
        self.assertFalse(order.opportunity_id)
        self.assertEqual(order.marketing_crm_lead_count, 0)
        action = order.action_view_marketing_crm_leads()
        self.assertEqual(action["domain"], [("id", "in", [])])
        self.assertNotIn("res_id", action)

    def test_native_lead_merge_preserves_history_and_links_the_survivor(self):
        source = self.env["crm.lead"].create(
            {
                "name": "Merged sales source",
                "company_id": self.env.company.id,
                "probability": 10,
            }
        )
        target = self.env["crm.lead"].create(
            {
                "name": "Merged sales survivor",
                "company_id": self.env.company.id,
                "probability": 90,
            }
        )
        order = self._create_order("Lead merge", source)
        historical_link = order.marketing_crm_link_ids
        source_id = source.id

        merged = (source | target).merge_opportunity()
        historical_link.invalidate_recordset()
        order.invalidate_recordset(["marketing_crm_lead_count", "opportunity_id"])

        self.assertEqual(merged, target)
        self.assertFalse(self.env["crm.lead"].browse(source_id).exists())
        self.assertFalse(historical_link.lead_id)
        self.assertEqual(historical_link.lead_res_id, source_id)
        self.assertEqual(historical_link.lead_ref, "Merged sales source")
        # Odoo's native ``sale_crm`` merge transfers ``order_ids`` to the merge
        # survivor.  The live association follows that native state, while our
        # append-only edge continues to preserve the deleted source identity.
        self.assertEqual(order.opportunity_id, target)
        self.assertEqual(order.marketing_crm_lead_count, 1)
        survivor_link = order.marketing_crm_link_ids.filtered(
            lambda link: link.lead_id == target
        )
        self.assertEqual(len(survivor_link), 1)
        self.assertEqual(len(order.marketing_crm_link_ids), 2)

        # Repeating the already canonical association is idempotent.
        order.write({"opportunity_id": target.id})
        live_links = order.marketing_crm_link_ids.filtered("lead_id")
        self.assertEqual(live_links.lead_id, target)
        self.assertEqual(len(order.marketing_crm_link_ids), 2)

    def test_cross_company_and_scope_mutation_are_rejected(self):
        other_company = self.env["res.company"].create({"name": "Sale Other Company"})
        other_lead = (
            self.env["crm.lead"]
            .with_context(allowed_company_ids=[self.env.company.id, other_company.id])
            .with_company(other_company)
            .create({"name": "Other Company Lead", "company_id": other_company.id})
        )
        with self.assertRaises(ValidationError):
            self.env["marketing.sale.service"].with_context(
                allowed_company_ids=[self.env.company.id, other_company.id]
            )._link_order_lead(self.order, other_lead, "cross-company")
        with self.assertRaises(ValidationError):
            self.order.with_context(
                allowed_company_ids=[self.env.company.id, other_company.id]
            ).write({"company_id": other_company.id})
        with self.assertRaises(AccessError):
            self.order.write({"marketing_sale_event_sequence": 999})

    def test_copy_gets_a_fresh_occurrence_sequence_and_no_event_edges(self):
        self.order.action_quotation_sent()
        self.assertEqual(self.order.marketing_sale_event_sequence, 1)
        copied = self.order.copy()
        self.assertEqual(copied.marketing_sale_event_sequence, 0)
        self.assertFalse(copied.marketing_event_link_ids)
        self.assertFalse(self._events(copied))

    def test_explicit_backfill_uses_stable_evidence_and_is_idempotent(self):
        order = self._create_order("Backfill")
        guarded = order.with_context(
            marketing_sale_transition_guard=MARKETING_SALE_TRANSITION_GUARD
        )
        guarded.action_quotation_sent()
        guarded.action_confirm()
        guarded.with_context(disable_cancel_warning=True)._action_cancel()
        self.assertFalse(self._events(order))

        # Odoo 16 does not necessarily create a tracking row for the
        # draft -> sent action.  Persist the two historical rows that prove the
        # quotation was already sent when it became an order and was later
        # cancelled.  The backfill must infer proposal_sent from the observed
        # old ``sent`` state, without fabricating events when no evidence exists.
        state_field = self.env["ir.model.fields"]._get("sale.order", "state")
        Tracking = self.env["mail.tracking.value"].sudo()
        for old_label, new_label in (
            ("Quotation Sent", "Sales Order"),
            ("Sales Order", "Cancelled"),
        ):
            message = order.message_post(
                body="Historical state evidence: %s -> %s" % (old_label, new_label)
            )
            Tracking.create(
                {
                    "field": state_field.id,
                    "field_desc": "Status",
                    "field_type": "selection",
                    "old_value_char": old_label,
                    "new_value_char": new_label,
                    "mail_message_id": message.id,
                }
            )

        service = self.env["marketing.sale.service"]
        result = service._backfill_order_events(order, limit=1)
        cursor = result["last_tracking_id"]
        while result["has_more"]:
            result = service._backfill_order_events(
                order, after_tracking_id=cursor, limit=1
            )
            self.assertGreater(result["last_tracking_id"], cursor)
            cursor = result["last_tracking_id"]
        first_ids = self._events(order).ids
        self.assertTrue(self._events(order, "proposal_sent"))
        self.assertTrue(self._events(order, "order_confirmed"))
        self.assertTrue(self._events(order, "order_cancelled"))

        cursor = 0
        while True:
            result = service._backfill_order_events(
                order, after_tracking_id=cursor, limit=1
            )
            if not result["has_more"]:
                break
            cursor = result["last_tracking_id"]
        self.assertEqual(self._events(order).ids, first_ids)

    def test_link_rules_follow_sale_ownership(self):
        sales_group = self.env.ref("sales_team.group_sale_salesman")
        owner = self._create_salesperson("owner", sales_group)
        outsider = self._create_salesperson("outsider", sales_group)
        self.order.write({"user_id": owner.id})
        self.order.action_confirm()
        crm_link = self.order.marketing_crm_link_ids[:1]
        event_link = self.order.marketing_event_link_ids[:1]
        crm_link.with_user(owner).read(["id"])
        event_link.with_user(owner).read(["id"])
        with self.assertRaises(AccessError):
            crm_link.with_user(outsider).read(["id"])
        with self.assertRaises(AccessError):
            event_link.with_user(outsider).read(["id"])

    @classmethod
    def _create_salesperson(cls, label, group):
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Marketing Sale %s" % label,
                    "login": "marketing-sale-%s-%s" % (label, uuid.uuid4()),
                    "email": "%s@example.invalid" % label,
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, group.ids)],
                }
            )
        )

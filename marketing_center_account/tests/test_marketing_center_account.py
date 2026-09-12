import datetime
import decimal
import uuid
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.marketing_center_account.models.tokens import (
    MARKETING_ACCOUNT_TRANSITION_GUARD,
)


class TestMarketingCenterAccount(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suffix = uuid.uuid4().hex[:8]
        cls.receivable = cls.env["account.account"].create(
            {
                "name": "Marketing Receivable %s" % suffix,
                "code": "MR%s" % suffix[:6].upper(),
                "account_type": "asset_receivable",
                "reconcile": True,
                "company_id": cls.env.company.id,
            }
        )
        cls.income = cls.env["account.account"].create(
            {
                "name": "Marketing Income %s" % suffix,
                "code": "MI%s" % suffix[:6].upper(),
                "account_type": "income",
                "company_id": cls.env.company.id,
            }
        )
        cls.bank_account = cls.env["account.account"].create(
            {
                "name": "Marketing Bank %s" % suffix,
                "code": "MB%s" % suffix[:6].upper(),
                "account_type": "asset_cash",
                "company_id": cls.env.company.id,
            }
        )
        cls.sales_journal = cls.env["account.journal"].create(
            {
                "name": "Marketing Sales %s" % suffix,
                "code": "S%s" % suffix[:4].upper(),
                "type": "sale",
                "company_id": cls.env.company.id,
            }
        )
        cls.bank_journal = cls.env["account.journal"].create(
            {
                "name": "Marketing Bank %s" % suffix,
                "code": "B%s" % suffix[:4].upper(),
                "type": "bank",
                "company_id": cls.env.company.id,
                "default_account_id": cls.bank_account.id,
            }
        )
        cls.bank_journal.inbound_payment_method_line_ids.write(
            {"payment_account_id": cls.bank_account.id}
        )
        cls.partner = cls.env["res.partner"].create(
            {"name": "Marketing Account Partner %s" % suffix}
        )
        cls.partner.with_company(
            cls.env.company
        ).property_account_receivable_id = cls.receivable
        foreign_code = "Z%s%s" % (
            chr(ord("A") + int(suffix[0], 16)),
            chr(ord("A") + int(suffix[1], 16)),
        )
        cls.foreign_currency = cls.env["res.currency"].create(
            {
                "name": foreign_code,
                "symbol": "Z$",
                "rounding": decimal.Decimal("0.01"),
            }
        )
        cls.env["res.currency.rate"].create(
            {
                "name": fields.Date.today(),
                "rate": decimal.Decimal("2.0"),
                "currency_id": cls.foreign_currency.id,
                "company_id": cls.env.company.id,
            }
        )

    def _invoice(
        self, amount=100, move_type="out_invoice", reversed_entry=None, currency=None
    ):
        values = {
            "move_type": move_type,
            "company_id": self.env.company.id,
            "journal_id": self.sales_journal.id,
            "partner_id": self.partner.id,
            "invoice_date": fields.Date.today(),
            "invoice_line_ids": [
                Command.create(
                    {
                        "name": "Marketing revenue",
                        "account_id": self.income.id,
                        "quantity": 1,
                        "price_unit": amount,
                    }
                )
            ],
        }
        if currency:
            values["currency_id"] = currency.id
        if reversed_entry:
            values["reversed_entry_id"] = reversed_entry.id
        return self.env["account.move"].create(values)

    def _payment(self, amount):
        payment_method = self.bank_journal.inbound_payment_method_line_ids[:1]
        self.assertTrue(payment_method)
        payment = self.env["account.payment"].create(
            {
                "payment_type": "inbound",
                "partner_type": "customer",
                "partner_id": self.partner.id,
                "amount": amount,
                "currency_id": self.env.company.currency_id.id,
                "date": fields.Date.today(),
                "journal_id": self.bank_journal.id,
                "payment_method_line_id": payment_method.id,
                "destination_account_id": self.receivable.id,
            }
        )
        payment.action_post()
        return payment

    def _reconcile(self, invoice, payment, guarded=False):
        invoice_line = invoice.line_ids.filtered(
            lambda line: line.account_id.account_type == "asset_receivable"
        )
        payment_line = payment.move_id.line_ids.filtered(
            lambda line: line.account_id == self.receivable
        )
        lines = invoice_line | payment_line
        if guarded:
            lines = lines.with_context(
                marketing_account_transition_guard=(MARKETING_ACCOUNT_TRANSITION_GUARD)
            )
        lines.reconcile()
        partial = self.env["account.partial.reconcile"].search(
            [
                "|",
                ("debit_move_id", "in", lines.ids),
                ("credit_move_id", "in", lines.ids),
            ],
            order="id desc",
            limit=1,
        )
        self.assertTrue(partial)
        return partial

    def _move_events(self, move, event_type=None):
        events = move.marketing_account_event_link_ids.mapped("event_id")
        return events.filtered(
            lambda event: not event_type or event.event_type == event_type
        )

    def _payment_events(self, payment, event_type=None):
        events = payment.marketing_account_event_link_ids.mapped("event_id")
        return events.filtered(
            lambda event: not event_type or event.event_type == event_type
        )

    def test_invoice_and_credit_note_are_exact_and_idempotent(self):
        invoice = self._invoice(decimal.Decimal("123.45"))
        invoice.action_post()
        posted = self._move_events(invoice, "invoice_posted")
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted.amount_signed_micros, 123_450_000)
        self.assertEqual(posted.currency_id, invoice.currency_id)
        self.assertEqual(
            posted.snapshot_json["extensions"]["account.amount_basis"], "untaxed"
        )

        service = self.env["marketing.account.service"]
        self.assertEqual(service._ensure_move_event(invoice), posted)
        self.assertEqual(len(self._move_events(invoice, "invoice_posted")), 1)

        credit = self._invoice(
            decimal.Decimal("23.45"), move_type="out_refund", reversed_entry=invoice
        )
        credit.action_post()
        credit_event = self._move_events(credit, "credit_note_posted")
        self.assertEqual(len(credit_event), 1)
        self.assertEqual(credit_event.amount_signed_micros, -23_450_000)
        self.assertEqual(credit_event.reverses_event_id, posted)
        self.assertIn(
            invoice,
            credit_event.account_move_link_ids.filtered(
                lambda link: link.role == "reversed_invoice"
            ).mapped("move_id"),
        )

    def test_zero_value_invoice_and_credit_keep_an_exact_amount(self):
        invoice = self._invoice(0)
        invoice.action_post()
        posted = self._move_events(invoice, "invoice_posted")
        self.assertTrue(posted.has_amount)
        self.assertEqual(posted.amount_signed_micros, 0)
        credit = self._invoice(0, move_type="out_refund", reversed_entry=invoice)
        credit.action_post()
        credit_event = self._move_events(credit, "credit_note_posted")
        self.assertTrue(credit_event.has_amount)
        self.assertEqual(credit_event.amount_signed_micros, 0)
        self.assertEqual(credit_event.reverses_event_id, posted)

    def test_each_payment_partial_has_an_exact_reversal(self):
        invoice = self._invoice(100)
        invoice.action_post()
        payment = self._payment(40)
        partial = self._reconcile(invoice, payment)
        allocation = self._payment_events(payment, "payment_allocated")
        self.assertEqual(len(allocation), 1)
        replayed, _facts = self.env[
            "marketing.account.service"
        ]._ensure_allocation_event(partial)
        self.assertEqual(replayed, allocation)
        self.assertEqual(len(self._payment_events(payment, "payment_allocated")), 1)
        self.assertTrue(partial.marketing_account_event_claimed)
        with self.assertRaises(AccessError):
            partial.write({"marketing_account_event_claimed": False})
        self.assertEqual(payment.marketing_account_company_id, self.env.company)
        with self.assertRaises(AccessError):
            payment.write({"marketing_account_company_id": self.env.company.id})
        expected = int(
            decimal.Decimal(str(self.env.company.currency_id.round(partial.amount)))
            * decimal.Decimal(1_000_000)
        )
        self.assertEqual(allocation.amount_signed_micros, expected)
        self.assertEqual(allocation.currency_id, self.env.company.currency_id)
        self.assertEqual(
            allocation.snapshot_json["extensions"]["account.allocation_basis"],
            "partial_reconcile_company_currency",
        )
        partial.unlink()
        reversal = self._payment_events(payment, "payment_allocation_reversed")
        self.assertEqual(len(reversal), 1)
        self.assertEqual(reversal.reverses_event_id, allocation)
        self.assertEqual(reversal.amount_signed_micros, -expected)

    def test_batch_unlink_emits_one_reversal_for_each_allocation(self):
        invoice = self._invoice(100)
        invoice.action_post()
        payments = self.env["account.payment"]
        partials = self.env["account.partial.reconcile"]
        allocations = self.env["marketing.business.event"]
        for amount in (30, 40):
            payment = self._payment(amount)
            payments |= payment
            partial = self._reconcile(invoice, payment)
            partials |= partial
            allocations |= self._payment_events(payment, "payment_allocated")

        partial_ids = set(partials.ids)
        partials.unlink()
        reversals = payments.mapped(
            "marketing_account_event_link_ids.event_id"
        ).filtered(lambda event: event.event_type == "payment_allocation_reversed")
        self.assertEqual(len(reversals), 2)
        self.assertEqual(set(reversals.mapped("source_res_id")), partial_ids)
        self.assertEqual(
            set(reversals.mapped("reverses_event_id").ids), set(allocations.ids)
        )
        self.assertEqual(
            sum(reversals.mapped("amount_signed_micros")),
            -sum(allocations.mapped("amount_signed_micros")),
        )

    def test_three_partial_payments_remain_three_distinct_facts(self):
        invoice = self._invoice(100)
        invoice.action_post()
        payments = self.env["account.payment"]
        partials = self.env["account.partial.reconcile"]
        for amount in (20, 30, 50):
            payment = self._payment(amount)
            payments |= payment
            partials |= self._reconcile(invoice, payment)
        allocations = payments.mapped(
            "marketing_account_event_link_ids.event_id"
        ).filtered(lambda event: event.event_type == "payment_allocated")
        self.assertEqual(len(allocations), 3)
        self.assertEqual(set(allocations.mapped("source_res_id")), set(partials.ids))
        self.assertEqual(sum(allocations.mapped("amount_signed_micros")), 100_000_000)

    def test_foreign_invoice_and_company_currency_allocation_stay_separate(self):
        invoice = self._invoice(
            decimal.Decimal("123.44"), currency=self.foreign_currency
        )
        invoice.action_post()
        invoice_event = self._move_events(invoice, "invoice_posted")
        self.assertEqual(invoice_event.currency_id, self.foreign_currency)
        self.assertEqual(invoice_event.amount_signed_micros, 123_440_000)

        payment = self._payment(abs(invoice.amount_residual_signed))
        partial = self._reconcile(invoice, payment)
        allocation = self._payment_events(payment, "payment_allocated")
        expected = int(
            decimal.Decimal(str(self.env.company.currency_id.round(partial.amount)))
            * decimal.Decimal(1_000_000)
        )
        self.assertEqual(allocation.currency_id, self.env.company.currency_id)
        self.assertEqual(allocation.amount_signed_micros, expected)
        self.assertEqual(
            allocation.snapshot_json["extensions"]["account.allocation_basis"],
            "partial_reconcile_company_currency",
        )

    def test_credit_offset_is_not_mislabelled_as_cash_receipt(self):
        invoice = self._invoice(100)
        invoice.action_post()
        credit = self._invoice(25, move_type="out_refund", reversed_entry=invoice)
        credit.action_post()
        receivable_lines = (invoice | credit).line_ids.filtered(
            lambda line: line.account_id.account_type == "asset_receivable"
        )
        receivable_lines.reconcile()
        partial = self.env["account.partial.reconcile"].search(
            [
                "|",
                ("debit_move_id", "in", receivable_lines.ids),
                ("credit_move_id", "in", receivable_lines.ids),
            ],
            order="id desc",
            limit=1,
        )
        self.assertTrue(partial)
        self.assertFalse(
            self.env["marketing.business.event"].search(
                [
                    ("source_model", "=", "account.partial.reconcile"),
                    ("source_res_id", "=", partial.id),
                ]
            )
        )

    def test_explicit_paginated_backfill_is_idempotent(self):
        invoice = self._invoice(55)
        invoice.with_context(
            marketing_account_transition_guard=MARKETING_ACCOUNT_TRANSITION_GUARD
        ).action_post()
        self.assertFalse(self._move_events(invoice))

        payment = self._payment(20)
        partial = self._reconcile(invoice, payment, guarded=True)
        self.assertFalse(self._payment_events(payment))
        service = self.env["marketing.account.service"]
        move_result = service._backfill_posted_moves(
            self.env.company, after_id=invoice.id - 1, limit=1
        )
        self.assertEqual(move_result["processed"], 1)
        partial_result = service._backfill_payment_allocations(
            self.env.company, after_id=partial.id - 1, limit=1
        )
        self.assertEqual(partial_result["processed"], 1)
        self.assertEqual(partial_result["emitted"], 1)
        first_move_events = self._move_events(invoice).ids
        first_payment_events = self._payment_events(payment).ids
        service._backfill_posted_moves(
            self.env.company, after_id=invoice.id - 1, limit=1
        )
        service._backfill_payment_allocations(
            self.env.company, after_id=partial.id - 1, limit=1
        )
        self.assertEqual(self._move_events(invoice).ids, first_move_events)
        self.assertEqual(self._payment_events(payment).ids, first_payment_events)

    def test_unlink_of_historical_partial_materializes_the_exact_pair(self):
        invoice = self._invoice(30)
        invoice.action_post()
        payment = self._payment(30)
        partial = self._reconcile(invoice, payment, guarded=True)
        self.assertFalse(self._payment_events(payment))
        partial.unlink()
        allocation = self._payment_events(payment, "payment_allocated")
        reversal = self._payment_events(payment, "payment_allocation_reversed")
        self.assertEqual(len(allocation), 1)
        self.assertEqual(len(reversal), 1)
        self.assertEqual(reversal.reverses_event_id, allocation)
        self.assertEqual(
            reversal.amount_signed_micros, -allocation.amount_signed_micros
        )

    def test_context_defaults_cannot_seed_accounting_event_identity(self):
        invoice = self._invoice(10)
        for name, value in (
            ("marketing_account_event_sequence", 999),
            ("marketing_account_company_id", self.env.company.id),
        ):
            with self.subTest(field=name), self.assertRaises(AccessError):
                invoice.with_context(**{"default_%s" % name: value}).copy()
        payment = self._payment(10)
        with self.assertRaises(AccessError):
            payment.with_context(
                default_marketing_account_company_id=self.env.company.id
            ).copy()
        invoice.action_post()
        lines = (invoice | payment.move_id).line_ids.filtered(
            lambda line: line.account_id == self.receivable
        )
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            lines.with_context(default_marketing_account_event_claimed=True).reconcile()

    def test_links_and_marketing_scope_are_immutable(self):
        invoice = self._invoice(10)
        invoice.action_post()
        event_link = invoice.marketing_account_event_link_ids[:1]
        with self.assertRaises(AccessError):
            event_link.sudo().write({"role": "settled_invoice"})
        with self.assertRaises(AccessError):
            event_link.sudo().unlink()
        with self.assertRaises(AccessError):
            invoice.write({"marketing_account_event_sequence": 999})
        copied = invoice.copy({"state": "draft", "name": "/"})
        self.assertEqual(copied.marketing_account_event_sequence, 0)
        self.assertFalse(copied.marketing_account_company_id)
        self.assertFalse(copied.marketing_account_event_link_ids)

        other_company = self.env["res.company"].create(
            {"name": "Other accounting scope %s" % uuid.uuid4().hex[:8]}
        )
        other_journal = self.env["account.journal"].create(
            {
                "name": "Other scope journal %s" % uuid.uuid4().hex[:8],
                "code": "O%s" % uuid.uuid4().hex[:4].upper(),
                "type": "general",
                "company_id": other_company.id,
            }
        )
        with self.assertRaises(ValidationError):
            invoice.write({"journal_id": other_journal.id})

    def test_deleted_invoice_leaves_event_and_bounded_native_snapshot(self):
        invoice = self._invoice(10)
        invoice.action_post()
        event = self._move_events(invoice, "invoice_posted")
        link = invoice.marketing_account_event_link_ids.filtered(
            lambda candidate: candidate.event_id == event and candidate.role == "source"
        )
        move_id = invoice.id
        move_ref = invoice.name

        invoice.button_draft()
        invoice.with_context(force_delete=True).unlink()
        link.invalidate_recordset()
        event.invalidate_recordset(["account_move_count"])

        self.assertFalse(self.env["account.move"].browse(move_id).exists())
        self.assertFalse(link.move_id)
        self.assertEqual(link.move_model, "account.move")
        self.assertEqual(link.move_res_id, move_id)
        self.assertEqual(link.move_ref, move_ref)
        self.assertEqual(event.account_move_count, 0)
        action = event.action_view_account_moves()
        self.assertEqual(action["domain"], [("id", "in", [])])
        self.assertNotIn("res_id", action)

    def test_deleted_payment_leaves_allocation_evidence_without_dangling_target(self):
        invoice = self._invoice(30)
        invoice.action_post()
        payment = self._payment(30)
        partial = self._reconcile(invoice, payment)
        allocation = self._payment_events(payment, "payment_allocated")
        payment_link = allocation.account_payment_link_ids.filtered(
            lambda link: link.payment_id == payment
        )
        payment_move_link = allocation.account_move_link_ids.filtered(
            lambda link: link.role == "payment_entry"
        )
        payment_id = payment.id
        payment_ref = payment.move_id.name
        payment_move_id = payment.move_id.id
        payment_move_ref = payment.move_id.name

        partial.unlink()
        payment.action_draft()
        payment.unlink()
        payment_link.invalidate_recordset()
        payment_move_link.invalidate_recordset()
        allocation.invalidate_recordset(["account_move_count", "account_payment_count"])

        self.assertFalse(self.env["account.payment"].browse(payment_id).exists())
        self.assertFalse(payment_link.payment_id)
        self.assertEqual(payment_link.payment_model, "account.payment")
        self.assertEqual(payment_link.payment_res_id, payment_id)
        self.assertEqual(payment_link.payment_ref, payment_ref)
        self.assertFalse(payment_move_link.move_id)
        self.assertEqual(payment_move_link.move_model, "account.move")
        self.assertEqual(payment_move_link.move_res_id, payment_move_id)
        self.assertEqual(payment_move_link.move_ref, payment_move_ref)
        self.assertEqual(allocation.account_payment_count, 0)
        payment_action = allocation.action_view_account_payments()
        self.assertEqual(payment_action["domain"], [("id", "in", [])])
        self.assertNotIn("res_id", payment_action)

    def test_readonly_accounting_user_does_not_receive_ledger_only_fields(self):
        readonly = self.env.ref("account.group_account_readonly")
        user = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Accounting readonly",
                    "login": "account-readonly-%s" % uuid.uuid4(),
                    "email": "account-readonly@example.invalid",
                    "company_id": self.env.company.id,
                    "company_ids": [Command.set(self.env.company.ids)],
                    "groups_id": [Command.set(readonly.ids)],
                }
            )
        )
        move_fields = self.env["account.move"].with_user(user).fields_get()
        payment_fields = self.env["account.payment"].with_user(user).fields_get()
        self.assertNotIn("marketing_account_event_link_ids", move_fields)
        self.assertNotIn("marketing_business_event_count", move_fields)
        self.assertNotIn("marketing_account_event_link_ids", payment_fields)
        self.assertNotIn("marketing_business_event_count", payment_fields)

    def test_link_rules_are_company_scoped(self):
        invoice = self._invoice(10)
        invoice.action_post()
        link = invoice.marketing_account_event_link_ids[:1]
        other_company = self.env["res.company"].create(
            {"name": "Marketing Account Other Company"}
        )
        analyst = self.env.ref("marketing_center_base.group_marketing_center_analyst")
        billing = self.env.ref("account.group_account_invoice")
        outsider = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Accounting outsider",
                    "login": "account-outsider-%s" % uuid.uuid4(),
                    "email": "account-outsider@example.invalid",
                    "company_id": other_company.id,
                    "company_ids": [Command.set(other_company.ids)],
                    "groups_id": [Command.set((analyst | billing).ids)],
                }
            )
        )
        with self.assertRaises(AccessError):
            link.with_user(outsider).read(["id"])

    def _posting_events(self, moves):
        return self.env["marketing.business.event"].search(
            [
                ("company_id", "=", self.env.company.id),
                ("source_system", "=", "odoo.account"),
                ("source_model", "=", "account.move"),
                ("source_res_id", "in", moves.ids),
            ],
            order="id",
        )

    def test_invoice_draft_repost_preserves_exact_history_and_current_value(self):
        invoice = self._invoice(100)
        invoice.action_post()
        first = self._posting_events(invoice)
        original_snapshot = first.snapshot_json
        invoice.button_draft()
        self.assertEqual(
            sum(self._posting_events(invoice).mapped("amount_signed_micros")), 0
        )
        self.assertFalse(
            self.env["marketing.account.service"]._ensure_move_event(invoice)
        )
        invoice.button_draft()
        self.assertEqual(len(self._posting_events(invoice)), 2)
        invoice.action_post()
        self.assertEqual(
            sum(self._posting_events(invoice).mapped("amount_signed_micros")),
            100_000_000,
        )
        invoice.button_draft()
        invoice.invoice_line_ids.write({"price_unit": 160})
        invoice.action_post()
        events = self._posting_events(invoice)
        self.assertEqual(len(events), 5)
        self.assertEqual(sum(events.mapped("amount_signed_micros")), 160_000_000)
        active = self.env["marketing.business.event.service"]._active_posting_events(
            events
        )
        self.assertEqual(len(active), 1)
        self.assertEqual(active.amount_signed_micros, 160_000_000)
        self.assertEqual(first.snapshot_json, original_snapshot)
        self.assertEqual(
            self.env["marketing.account.service"]._ensure_move_event(invoice), active
        )

    def test_full_credit_draft_repost_does_not_block_native_posting(self):
        invoice = self._invoice(100)
        invoice.action_post()
        credit = self._invoice(100, move_type="out_refund", reversed_entry=invoice)
        credit.action_post()
        original_credit = self._posting_events(credit)
        credit.button_draft()
        reversal = self._posting_events(credit).filtered(
            lambda event: event.event_type == "credit_note_posting_reversed"
        )
        self.assertEqual(reversal.reverses_event_id, original_credit)
        self.assertEqual(reversal.root_event_id, self._posting_events(invoice))
        self.assertEqual(reversal.amount_signed_micros, 100_000_000)
        credit.action_post()
        self.assertEqual(credit.state, "posted")
        events = self._posting_events(invoice | credit)
        self.assertEqual(sum(events.mapped("amount_signed_micros")), 0)
        self.assertEqual(len(self._posting_events(credit)), 3)
        credit.button_draft()
        credit.invoice_line_ids.write({"price_unit": 30})
        credit.action_post()
        another = self._invoice(70, move_type="out_refund", reversed_entry=invoice)
        another.action_post()
        self.assertEqual(
            sum(
                self._posting_events(invoice | credit | another).mapped(
                    "amount_signed_micros"
                )
            ),
            0,
        )
        self.assertTrue(self._posting_events(another).reverses_event_id)

    def test_invoice_repost_keeps_old_active_credits_and_native_excess_credit(self):
        invoice = self._invoice(100)
        invoice.action_post()
        original_posting = self._posting_events(invoice)
        first_credit = self._invoice(60, move_type="out_refund", reversed_entry=invoice)
        first_credit.action_post()
        invoice.button_draft()
        invoice.invoice_line_ids.write({"price_unit": 80})
        invoice.action_post()
        self.assertEqual(
            self._posting_events(first_credit).reverses_event_id, original_posting
        )
        second_credit = self._invoice(
            30, move_type="out_refund", reversed_entry=invoice
        )
        second_credit.action_post()
        event = self._posting_events(second_credit)
        self.assertEqual(second_credit.state, "posted")
        self.assertFalse(event.reverses_event_id)
        self.assertEqual(
            event.snapshot_json["extensions"]["account.credit_reversal_disposition"],
            "exceeds_active_invoice_amount",
        )
        self.assertIn(
            invoice,
            event.account_move_link_ids.filtered(
                lambda link: link.role == "reversed_invoice"
            ).mapped("move_id"),
        )
        self.assertEqual(
            sum(
                self._posting_events(invoice | first_credit | second_credit).mapped(
                    "amount_signed_micros"
                )
            ),
            -10_000_000,
        )
        # Reversing an independent (uncapped) credit restores its exact amount.
        second_credit.button_draft()
        self.assertEqual(
            sum(
                self._posting_events(invoice | first_credit | second_credit).mapped(
                    "amount_signed_micros"
                )
            ),
            20_000_000,
        )

    def test_credit_against_draft_invoice_retains_fact_and_typed_relation(self):
        invoice = self._invoice(100)
        invoice.action_post()
        invoice.button_draft()
        credit = self._invoice(20, move_type="out_refund", reversed_entry=invoice)
        credit.action_post()
        event = self._posting_events(credit)
        self.assertFalse(event.reverses_event_id)
        self.assertEqual(
            event.snapshot_json["extensions"]["account.credit_reversal_disposition"],
            "target_not_posted",
        )
        self.assertEqual(
            sum(self._posting_events(invoice | credit).mapped("amount_signed_micros")),
            -20_000_000,
        )

    def test_direct_cancel_and_repeated_cancel_have_one_exact_counterevent(self):
        invoice = self._invoice(40)
        invoice.action_post()
        invoice.button_cancel()
        invoice.button_cancel()
        events = self._posting_events(invoice)
        self.assertEqual(invoice.state, "cancel")
        self.assertEqual(len(events), 2)
        self.assertEqual(sum(events.mapped("amount_signed_micros")), 0)
        invoice.button_draft()
        invoice.action_post()
        self.assertEqual(
            sum(self._posting_events(invoice).mapped("amount_signed_micros")),
            40_000_000,
        )

    def test_backfill_then_unpost_and_repost_does_not_resurrect_old_occurrence(self):
        invoice = self._invoice(55)
        invoice.with_context(
            marketing_account_transition_guard=MARKETING_ACCOUNT_TRANSITION_GUARD
        ).action_post()
        service = self.env["marketing.account.service"]
        original = service._ensure_move_event(invoice)
        self.assertEqual(original.evidence_level, "imported")
        self.assertEqual(
            original.snapshot_json["extensions"]["account.occurred_at_basis"],
            "document_date",
        )
        invoice.button_draft()
        invoice.action_post()
        current = service._ensure_move_event(invoice)
        self.assertNotEqual(current, original)
        self.assertEqual(
            current.snapshot_json["extensions"]["account.occurred_at_basis"],
            "observed_transition",
        )
        self.assertEqual(len(self._posting_events(invoice)), 3)
        self.assertEqual(
            sum(self._posting_events(invoice).mapped("amount_signed_micros")),
            55_000_000,
        )

    def test_period_flows_and_currency_scopes_include_exact_counterevents(self):
        invoice = self._invoice(100)
        with patch(
            "odoo.fields.Datetime.now",
            return_value=datetime.datetime(2026, 8, 31, 23, 59),
        ):
            invoice.action_post()
        with patch(
            "odoo.fields.Datetime.now", return_value=datetime.datetime(2026, 9, 1, 0, 1)
        ):
            invoice.button_draft()
        invoice.currency_id = self.foreign_currency
        with patch(
            "odoo.fields.Datetime.now", return_value=datetime.datetime(2026, 9, 1, 0, 2)
        ):
            invoice.action_post()
        events = self._posting_events(invoice)
        september = events.filtered(
            lambda event: event.occurred_at >= datetime.datetime(2026, 9, 1)
        )
        company_currency = september.filtered(
            lambda event: event.currency_id == self.env.company.currency_id
        )
        foreign_currency = september.filtered(
            lambda event: event.currency_id == self.foreign_currency
        )
        self.assertEqual(
            sum(company_currency.mapped("amount_signed_micros")), -100_000_000
        )
        self.assertEqual(
            sum(foreign_currency.mapped("amount_signed_micros")), 100_000_000
        )
        self.assertEqual(
            sum(
                events.filtered(
                    lambda event: event.currency_id == self.env.company.currency_id
                ).mapped("amount_signed_micros")
            ),
            0,
        )

    def test_credit_other_currency_is_independent_without_blocking_post(self):
        invoice = self._invoice(100)
        invoice.action_post()
        credit = self._invoice(
            25,
            move_type="out_refund",
            reversed_entry=invoice,
            currency=self.foreign_currency,
        )
        credit.action_post()
        event = self._posting_events(credit)
        self.assertEqual(credit.state, "posted")
        self.assertFalse(event.reverses_event_id)
        self.assertEqual(event.currency_id, self.foreign_currency)
        self.assertEqual(
            event.snapshot_json["extensions"]["account.credit_reversal_disposition"],
            "different_currency",
        )
        credit.button_draft()
        self.assertEqual(
            sum(self._posting_events(credit).mapped("amount_signed_micros")), 0
        )

    def test_native_unposting_rejection_leaves_the_posting_effective(self):
        self.sales_journal.restrict_mode_hash_table = True
        invoice = self._invoice(100)
        invoice.action_post()
        posting = self._posting_events(invoice)
        with self.assertRaises(UserError), self.env.cr.savepoint():
            invoice.button_draft()
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(self._posting_events(invoice), posting)
        self.assertEqual(
            self.env["marketing.business.event.service"]._active_posting_events(
                posting
            ),
            posting,
        )

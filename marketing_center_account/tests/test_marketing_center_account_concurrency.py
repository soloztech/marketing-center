import threading
import uuid

from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api, fields
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.marketing_center_account.models.tokens import (
    MARKETING_ACCOUNT_TRANSITION_GUARD,
)


@tagged("-at_install", "post_install")
class TestMarketingCenterAccountConcurrency(TransactionCase):
    WORKER_TIMEOUT_SECONDS = 15

    def _setup_committed_partial(self):
        token = uuid.uuid4().hex[:8]
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            company = env.company
            receivable = env["account.account"].create(
                {
                    "name": "Concurrent receivable %s" % token,
                    "code": "CR%s" % token[:6].upper(),
                    "account_type": "asset_receivable",
                    "reconcile": True,
                    "company_id": company.id,
                }
            )
            income = env["account.account"].create(
                {
                    "name": "Concurrent income %s" % token,
                    "code": "CI%s" % token[:6].upper(),
                    "account_type": "income",
                    "company_id": company.id,
                }
            )
            bank = env["account.account"].create(
                {
                    "name": "Concurrent bank %s" % token,
                    "code": "CB%s" % token[:6].upper(),
                    "account_type": "asset_cash",
                    "company_id": company.id,
                }
            )
            sales_journal = env["account.journal"].create(
                {
                    "name": "Concurrent sales %s" % token,
                    "code": "C%s" % token[:4].upper(),
                    "type": "sale",
                    "company_id": company.id,
                }
            )
            bank_journal = env["account.journal"].create(
                {
                    "name": "Concurrent bank %s" % token,
                    "code": "D%s" % token[:4].upper(),
                    "type": "bank",
                    "company_id": company.id,
                    "default_account_id": bank.id,
                }
            )
            bank_journal.inbound_payment_method_line_ids.write(
                {"payment_account_id": bank.id}
            )
            partner = env["res.partner"].create(
                {"name": "Concurrent partner %s" % token}
            )
            partner.with_company(company).property_account_receivable_id = receivable
            invoice = env["account.move"].create(
                {
                    "move_type": "out_invoice",
                    "company_id": company.id,
                    "journal_id": sales_journal.id,
                    "partner_id": partner.id,
                    "invoice_date": fields.Date.today(),
                    "invoice_line_ids": [
                        (
                            0,
                            0,
                            {
                                "name": "Concurrent revenue",
                                "account_id": income.id,
                                "quantity": 1,
                                "price_unit": 100,
                            },
                        )
                    ],
                }
            )
            invoice.with_context(
                marketing_account_transition_guard=(MARKETING_ACCOUNT_TRANSITION_GUARD)
            ).action_post()
            method = bank_journal.inbound_payment_method_line_ids[:1]
            payment = env["account.payment"].create(
                {
                    "payment_type": "inbound",
                    "partner_type": "customer",
                    "partner_id": partner.id,
                    "amount": 40,
                    "currency_id": company.currency_id.id,
                    "date": invoice.invoice_date,
                    "journal_id": bank_journal.id,
                    "payment_method_line_id": method.id,
                    "destination_account_id": receivable.id,
                }
            )
            payment.action_post()
            invoice_line = invoice.line_ids.filtered(
                lambda line: line.account_id == receivable
            )
            payment_line = payment.move_id.line_ids.filtered(
                lambda line: line.account_id == receivable
            )
            (invoice_line | payment_line).with_context(
                marketing_account_transition_guard=(MARKETING_ACCOUNT_TRANSITION_GUARD)
            ).reconcile()
            partial = env["account.partial.reconcile"].search(
                [
                    "|",
                    ("debit_move_id", "in", (invoice_line | payment_line).ids),
                    ("credit_move_id", "in", (invoice_line | payment_line).ids),
                ],
                order="id desc",
                limit=1,
            )
            self.assertTrue(partial)
            self.assertFalse(partial.marketing_account_event_claimed)
            self.assertFalse(
                env["marketing.business.event"].search(
                    [
                        ("source_model", "=", "account.partial.reconcile"),
                        ("source_res_id", "=", partial.id),
                    ]
                )
            )
            cr.commit()  # pylint: disable=invalid-commit
            return {
                "account_ids": (receivable | income | bank).ids,
                "company_id": company.id,
                "invoice_id": invoice.id,
                "journal_ids": (sales_journal | bank_journal).ids,
                "partial_id": partial.id,
                "partner_id": partner.id,
                "payment_id": payment.id,
            }

    def _cleanup_committed_fixture(self, fixture):
        """Remove records committed solely to make the two sessions observable."""
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            cr.execute(
                "SELECT id FROM marketing_business_event "
                "WHERE source_system = 'odoo.account' "
                "AND ((source_model = 'account.partial.reconcile' AND source_res_id = %s) "
                "OR (source_model = 'account.move' AND source_res_id = %s))",
                [fixture["partial_id"], fixture["invoice_id"]],
            )
            event_ids = [row[0] for row in cr.fetchall()]
            if event_ids:
                # Optional glue projections are immutable as well. Test cleanup
                # therefore removes this private fixture from leaves to root.
                cr.execute("SELECT to_regclass('marketing_sale_account_projection')")
                if cr.fetchone()[0]:
                    cr.execute(
                        "DELETE FROM marketing_sale_account_projection "
                        "WHERE account_link_id IN ("
                        "SELECT id "
                        "FROM marketing_business_event_account_move_link "
                        "WHERE event_id = ANY(%s))",
                        [event_ids],
                    )
                cr.execute(
                    "DELETE FROM marketing_business_event_account_payment_link "
                    "WHERE event_id = ANY(%s)",
                    [event_ids],
                )
                cr.execute(
                    "DELETE FROM marketing_business_event_account_move_link "
                    "WHERE event_id = ANY(%s)",
                    [event_ids],
                )
                cr.execute(
                    "DELETE FROM marketing_business_event_observation "
                    "WHERE event_id = ANY(%s)",
                    [event_ids],
                )
                cr.execute(
                    "DELETE FROM marketing_business_event WHERE id = ANY(%s)",
                    [event_ids],
                )

            partial = env["account.partial.reconcile"].browse(fixture["partial_id"])
            partial.exists().with_context(
                marketing_account_transition_guard=(MARKETING_ACCOUNT_TRANSITION_GUARD)
            ).unlink()
            payment = env["account.payment"].browse(fixture["payment_id"]).exists()
            if payment:
                payment.action_draft()
                payment.unlink()
            invoice = env["account.move"].browse(fixture["invoice_id"]).exists()
            if invoice:
                invoice.with_context(
                    marketing_account_transition_guard=MARKETING_ACCOUNT_TRANSITION_GUARD
                ).button_draft()
                invoice.with_context(force_delete=True).unlink()
            env["res.partner"].browse(fixture["partner_id"]).exists().unlink()
            env["account.journal"].browse(fixture["journal_ids"]).exists().unlink()
            env["account.account"].browse(fixture["account_ids"]).exists().unlink()
            self.assertFalse(
                env["account.partial.reconcile"].browse(fixture["partial_id"]).exists()
            )
            self.assertFalse(
                env["account.payment"].browse(fixture["payment_id"]).exists()
            )
            self.assertFalse(env["account.move"].browse(fixture["invoice_id"]).exists())
            self.assertFalse(env["res.partner"].browse(fixture["partner_id"]).exists())
            cr.commit()  # pylint: disable=invalid-commit

    def _retry_projection(self, fixture):
        with self.registry.cursor() as cr:
            cr.execute("SET LOCAL lock_timeout = '5s'")
            cr.execute("SET LOCAL statement_timeout = '10s'")
            env = api.Environment(cr, SUPERUSER_ID, {})
            event, _facts = env["marketing.account.service"]._ensure_allocation_event(
                env["account.partial.reconcile"].browse(fixture["partial_id"])
            )
            cr.commit()  # pylint: disable=invalid-commit
            return event.id

    def _project_concurrently(self, fixture, barrier, results, errors):
        try:
            with self.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = '5s'")
                cr.execute("SET LOCAL statement_timeout = '10s'")
                env = api.Environment(cr, SUPERUSER_ID, {})
                partial = env["account.partial.reconcile"].browse(fixture["partial_id"])
                # Establish the same pre-claim repeatable-read snapshot.
                if partial.marketing_account_event_claimed:
                    raise AssertionError("The allocation was claimed before the race.")
                barrier.wait(timeout=self.WORKER_TIMEOUT_SECONDS)
                try:
                    event, _facts = env[
                        "marketing.account.service"
                    ]._ensure_allocation_event(partial)
                    cr.commit()  # pylint: disable=invalid-commit
                    results.append(("committed", event.id))
                except SerializationFailure:
                    cr.rollback()
                    results.append(("retried", self._retry_projection(fixture)))
        except Exception as error:  # surface worker failures in the main thread
            errors.append(error)

    def test_concurrent_first_projection_converges_to_one_allocation_event(self):
        fixture = self._setup_committed_partial()
        try:
            barrier = threading.Barrier(2)
            results = []
            errors = []
            workers = [
                threading.Thread(
                    target=self._project_concurrently,
                    args=(fixture, barrier, results, errors),
                    name="marketing-account-projector-%s" % index,
                    daemon=True,
                )
                for index in range(2)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(self.WORKER_TIMEOUT_SECONDS)
            live_workers = [worker.name for worker in workers if worker.is_alive()]
            if live_workers:
                barrier.abort()
                for worker in workers:
                    worker.join(self.WORKER_TIMEOUT_SECONDS)
                self.fail(
                    "Concurrent accounting workers did not finish: %s" % live_workers
                )
            if errors:
                raise errors[0]

            self.assertEqual(len(results), 2)
            self.assertEqual(len({event_id for _state, event_id in results}), 1)
            self.assertTrue(
                all(state in {"committed", "retried"} for state, _id in results)
            )
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                partial = env["account.partial.reconcile"].browse(fixture["partial_id"])
                events = env["marketing.business.event"].search(
                    [
                        ("source_model", "=", "account.partial.reconcile"),
                        ("source_res_id", "=", partial.id),
                        ("event_type", "=", "payment_allocated"),
                    ]
                )
                self.assertTrue(partial.marketing_account_event_claimed)
                self.assertEqual(len(events), 1)
        finally:
            self._cleanup_committed_fixture(fixture)

    def _project_invoice_concurrently(self, fixture, barrier, results, errors, mode):
        try:
            for attempt in range(3):
                with self.registry.cursor() as cr:
                    cr.execute("SET LOCAL lock_timeout = '5s'")
                    cr.execute("SET LOCAL statement_timeout = '10s'")
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    invoice = env["account.move"].browse(fixture["invoice_id"])
                    if attempt == 0:
                        # Both transactions start from the same posted snapshot.
                        self.assertEqual(invoice.state, "posted")
                        barrier.wait(timeout=self.WORKER_TIMEOUT_SECONDS)
                    try:
                        if mode == "draft":
                            invoice.button_draft()
                            event_id = False
                        else:
                            event_id = (
                                env["marketing.account.service"]
                                ._ensure_move_event(invoice)
                                .id
                            )
                        cr.commit()  # pylint: disable=invalid-commit
                        results.append((mode, event_id))
                        return
                    except SerializationFailure:
                        cr.rollback()
            raise AssertionError(
                "Invoice projection exhausted its transaction retries."
            )
        except Exception as error:
            errors.append(error)

    def _run_invoice_race(self, fixture, modes):
        barrier = threading.Barrier(2)
        results, errors = [], []
        workers = [
            threading.Thread(
                target=self._project_invoice_concurrently,
                args=(fixture, barrier, results, errors, mode),
                name="marketing-invoice-%s-%s" % (mode, index),
                daemon=True,
            )
            for index, mode in enumerate(modes)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(self.WORKER_TIMEOUT_SECONDS)
        if any(worker.is_alive() for worker in workers):
            barrier.abort()
            for worker in workers:
                worker.join(self.WORKER_TIMEOUT_SECONDS)
            self.fail("Concurrent invoice workers did not finish.")
        if errors:
            raise errors[0]
        self.assertEqual(len(results), 2)
        return results

    def test_concurrent_invoice_backfill_has_one_active_posting(self):
        fixture = self._setup_committed_partial()
        try:
            results = self._run_invoice_race(fixture, ("backfill", "backfill"))
            self.assertEqual(len({event_id for _mode, event_id in results}), 1)
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                events = env["marketing.business.event"].search(
                    [
                        ("source_system", "=", "odoo.account"),
                        ("source_model", "=", "account.move"),
                        ("source_res_id", "=", fixture["invoice_id"]),
                    ]
                )
                self.assertEqual(len(events), 1)
                self.assertEqual(events.amount_signed_micros, 100_000_000)
        finally:
            self._cleanup_committed_fixture(fixture)

    def test_concurrent_invoice_backfill_and_unposting_end_with_zero_balance(self):
        fixture = self._setup_committed_partial()
        try:
            self._run_invoice_race(fixture, ("backfill", "draft"))
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                invoice = env["account.move"].browse(fixture["invoice_id"])
                self.assertEqual(invoice.state, "draft")
                events = env["marketing.business.event"].search(
                    [
                        ("source_system", "=", "odoo.account"),
                        ("source_model", "=", "account.move"),
                        ("source_res_id", "=", invoice.id),
                    ]
                )
                self.assertEqual(len(events), 2)
                self.assertEqual(sum(events.mapped("amount_signed_micros")), 0)
                self.assertFalse(
                    env["marketing.business.event.service"]._active_posting_events(
                        events
                    )
                )
        finally:
            self._cleanup_committed_fixture(fixture)

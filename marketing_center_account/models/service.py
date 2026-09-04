import decimal

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.marketing_center_base.services import MarketingBusinessEventDTO

from .tokens import (
    MARKETING_ACCOUNT_INTERNAL_WRITE_TOKEN,
    MARKETING_ACCOUNT_LINK_WRITE_TOKEN,
)

OUTGOING_MOVE_TYPES = frozenset({"out_invoice", "out_refund"})


class MarketingAccountService(models.AbstractModel):
    _name = "marketing.account.service"
    _description = "Marketing Accounting Integration Service"

    @api.model
    def _company_for_move(self, move):
        move = move.exists()
        if getattr(move, "_name", "") != "account.move" or len(move) != 1:
            raise ValidationError(_("A single valid journal entry is required."))
        self.env.cr.execute(
            "SELECT id FROM account_move WHERE id = %s FOR UPDATE", [move.id]
        )
        move.invalidate_recordset(["company_id", "marketing_account_company_id"])
        company = move.marketing_account_company_id or move.company_id
        if not company or company not in self.env.companies:
            raise AccessError(_("The accounting company is not available."))
        if move.company_id != company:
            raise ValidationError(
                _("The journal entry company conflicts with its marketing scope.")
            )
        if not move.marketing_account_company_id:
            move.with_context(
                marketing_account_internal_write_token=(
                    MARKETING_ACCOUNT_INTERNAL_WRITE_TOKEN
                )
            ).write({"marketing_account_company_id": company.id})
        return company

    @api.model
    def _payment_company_snapshot(self, payment):
        payment = payment.exists()
        if getattr(payment, "_name", "") != "account.payment" or len(payment) != 1:
            raise ValidationError(_("A single valid payment is required."))
        company = payment.marketing_account_company_id or payment.company_id
        if not company or company not in self.env.companies:
            raise AccessError(_("The payment company is not available."))
        if payment.company_id != company:
            raise ValidationError(
                _("The payment company conflicts with its marketing scope.")
            )
        return payment, company

    @api.model
    def _company_for_payment(self, payment):
        payment, _company = self._payment_company_snapshot(payment)
        self.env.cr.execute(
            "SELECT id FROM account_payment WHERE id = %s FOR UPDATE", [payment.id]
        )
        payment.invalidate_recordset(
            ["company_id", "marketing_account_company_id", "state"]
        )
        company = payment.marketing_account_company_id or payment.company_id
        if not company or company not in self.env.companies:
            raise AccessError(_("The payment company is not available."))
        if payment.company_id != company:
            raise ValidationError(
                _("The payment company conflicts with its marketing scope.")
            )
        if not payment.marketing_account_company_id:
            payment.with_context(
                marketing_account_internal_write_token=(
                    MARKETING_ACCOUNT_INTERNAL_WRITE_TOKEN
                )
            ).write({"marketing_account_company_id": company.id})
        return company

    @api.model
    def _lock_partial(self, partial):
        partial = partial.exists()
        if (
            getattr(partial, "_name", "") != "account.partial.reconcile"
            or len(partial) != 1
        ):
            return self.env["account.partial.reconcile"]
        self.env.cr.execute(
            "SELECT id FROM account_partial_reconcile WHERE id = %s FOR UPDATE",
            [partial.id],
        )
        if not self.env.cr.fetchone():
            return self.env["account.partial.reconcile"]
        partial.invalidate_recordset(
            [
                "amount",
                "company_id",
                "company_currency_id",
                "credit_move_id",
                "debit_move_id",
                "max_date",
                "marketing_account_event_claimed",
            ]
        )
        if partial.company_id not in self.env.companies:
            raise AccessError(_("The payment allocation company is not available."))
        return partial

    @api.model
    def _claim_partial_event(self, partial):
        partial = partial.exists()
        if not partial or len(partial) != 1:
            raise ValidationError(
                _("A valid payment allocation is required for its event claim.")
            )
        if not partial.marketing_account_event_claimed:
            partial.with_context(
                marketing_account_internal_write_token=(
                    MARKETING_ACCOUNT_INTERNAL_WRITE_TOKEN
                )
            ).write({"marketing_account_event_claimed": True})
        return partial

    @api.model
    def _decimal_amount(self, currency, value, negative=False):
        rounded = currency.round(value)
        amount = decimal.Decimal(str(rounded)).quantize(decimal.Decimal("0.000001"))
        amount = abs(amount)
        return -amount if negative else amount

    @api.model
    def _amount_micros(self, currency, value, negative=False):
        return int(
            self._decimal_amount(currency, value, negative=negative)
            * decimal.Decimal(1_000_000)
        )

    @api.model
    def _link_event_move(self, event, move, role="source"):
        event = event.exists()
        if getattr(event, "_name", "") != "marketing.business.event" or len(event) != 1:
            raise ValidationError(_("A single valid marketing event is required."))
        company = self._company_for_move(move)
        if event.company_id != company:
            raise ValidationError(
                _("Journal entries and marketing events cannot cross companies.")
            )
        if role not in {
            "source",
            "reversed_invoice",
            "settled_invoice",
            "payment_entry",
        }:
            raise ValidationError(_("The accounting event-link role is invalid."))
        lock_key = "marketing_account_event_move:%s:%s:%s:%s" % (
            company.id,
            move.id,
            event.id,
            role,
        )
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [lock_key]
        )
        Link = self.env["marketing.business.event.account.move.link"].sudo()
        link = Link.search(
            [
                ("company_id", "=", company.id),
                ("move_id", "=", move.id),
                ("event_id", "=", event.id),
                ("role", "=", role),
            ],
            limit=1,
        )
        created = False
        if not link:
            link = (
                Link.with_company(company)
                .with_context(
                    marketing_account_link_write_token=(
                        MARKETING_ACCOUNT_LINK_WRITE_TOKEN
                    )
                )
                .create(
                    {
                        "company_id": company.id,
                        "move_id": move.id,
                        "event_id": event.id,
                        "role": role,
                    }
                )
            )
            created = True
        self._after_link_event_move(event, move, role, link, created)
        return link

    @api.model
    def _after_link_event_move(self, event, move, role, link, created):
        """Extension point for optional causal bridges such as Sale/CRM."""
        return True

    @api.model
    def _link_event_payment(self, event, payment):
        event = event.exists()
        if getattr(event, "_name", "") != "marketing.business.event" or len(event) != 1:
            raise ValidationError(_("A single valid marketing event is required."))
        company = self._company_for_payment(payment)
        if event.company_id != company:
            raise ValidationError(
                _("Payments and marketing events cannot cross companies.")
            )
        lock_key = "marketing_account_event_payment:%s:%s:%s" % (
            company.id,
            payment.id,
            event.id,
        )
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [lock_key]
        )
        Link = self.env["marketing.business.event.account.payment.link"].sudo()
        link = Link.search(
            [
                ("company_id", "=", company.id),
                ("payment_id", "=", payment.id),
                ("event_id", "=", event.id),
            ],
            limit=1,
        )
        if not link:
            link = (
                Link.with_company(company)
                .with_context(
                    marketing_account_link_write_token=(
                        MARKETING_ACCOUNT_LINK_WRITE_TOKEN
                    )
                )
                .create(
                    {
                        "company_id": company.id,
                        "payment_id": payment.id,
                        "event_id": event.id,
                    }
                )
            )
        return link

    @api.model
    def _move_event_key(self, move, occurrence_ref):
        key = "account.move:%s:posted:%s" % (move.id, occurrence_ref)
        if len(key) > 512:
            raise ValidationError(_("The invoice event identity is too long."))
        return key

    @api.model
    def _source_move_events(self, move, event_type=None):
        company = self._company_for_move(move)
        domain = [
            ("company_id", "=", company.id),
            ("source_system", "=", "odoo.account"),
            ("source_model", "=", "account.move"),
            ("source_res_id", "=", move.id),
        ]
        if event_type:
            domain.append(("event_type", "=", event_type))
        events = (
            self.env["marketing.business.event"]
            .sudo()
            .search(domain, order="occurred_at asc, id asc")
        )
        for event in events:
            self._link_event_move(event, move, "source")
        return events

    @api.model
    def _move_extensions(self, move, sequence=0):
        currency = move.currency_id
        values = {
            "account.amount_basis": "untaxed",
            "account.amount_total_micros": self._amount_micros(
                currency, move.amount_total
            ),
            "account.amount_untaxed_micros": self._amount_micros(
                currency, move.amount_untaxed
            ),
            "account.currency": currency.name,
            "account.journal_id": move.journal_id.id,
            "account.move_name": move.name or "/",
            "account.move_type": move.move_type,
            "account.partner_id": move.partner_id.id or 0,
        }
        if sequence:
            values["account.transition_sequence"] = sequence
        if move.reversed_entry_id:
            values["account.reversed_move_id"] = move.reversed_entry_id.id
        return values

    @api.model
    def _emit_move_event(
        self,
        move,
        occurrence_ref,
        occurred_at=None,
        sequence=0,
        evidence_level="first_party",
        evidence_ref="",
    ):
        company = self._company_for_move(move)
        if move.state != "posted" or move.move_type not in OUTGOING_MOVE_TYPES:
            return self.env["marketing.business.event"]
        occurrence_ref = (occurrence_ref or "").strip()
        if not occurrence_ref or len(occurrence_ref) > 512:
            raise ValidationError(_("The invoice event occurrence is invalid."))
        event_type = (
            "invoice_posted"
            if move.move_type == "out_invoice"
            else "credit_note_posted"
        )
        negative = event_type == "credit_note_posted"
        amount = self._decimal_amount(
            move.currency_id, move.amount_untaxed, negative=negative
        )
        reversed_event = self.env["marketing.business.event"]
        if negative and move.reversed_entry_id:
            original = move.reversed_entry_id
            if original.company_id != company:
                raise ValidationError(
                    _("A credit note cannot reverse another company.")
                )
            reversed_event = self._ensure_move_event(
                original, evidence_level="imported"
            )
            if reversed_event and reversed_event.event_type != "invoice_posted":
                raise ValidationError(_("The credit note reversal target is invalid."))
        source_evidence_ref = "account.move:%s:%s" % (
            move.id,
            evidence_ref or occurrence_ref,
        )
        dto = MarketingBusinessEventDTO(
            event_class="revenue",
            event_type=event_type,
            source_system="odoo.account",
            source_model="account.move",
            source_res_id=move.id,
            source_occurrence_ref=occurrence_ref,
            source_evidence_ref=source_evidence_ref,
            business_event_key=self._move_event_key(move, occurrence_ref),
            occurred_at=occurred_at or fields.Datetime.now(),
            evidence_level=evidence_level,
            amount_signed=amount,
            currency=move.currency_id.name,
            reverses_business_event_key=(
                reversed_event.business_event_key if reversed_event else ""
            ),
            extensions=self._move_extensions(move, sequence=sequence),
        )
        result = (
            self.env["marketing.business.event.service"]
            .with_company(company)
            ._ingest_event(company, dto)
        )
        event = self.env["marketing.business.event"].sudo().browse(result.event_id)
        self._link_event_move(event, move, "source")
        if reversed_event:
            self._link_event_move(event, move.reversed_entry_id, "reversed_invoice")
        return event

    @api.model
    def _ensure_move_event(self, move, evidence_level="imported"):
        events = self._source_move_events(move)
        expected_type = {
            "out_invoice": "invoice_posted",
            "out_refund": "credit_note_posted",
        }.get(move.move_type)
        matching = events.filtered(lambda event: event.event_type == expected_type)
        if matching:
            return matching[-1]
        if move.state != "posted" or not expected_type:
            return self.env["marketing.business.event"]
        occurred_at = fields.Datetime.to_datetime(
            move.invoice_date or move.date or move.create_date or fields.Date.today()
        )
        stable_stamp = fields.Datetime.to_string(occurred_at).replace(" ", "T")
        return self._emit_move_event(
            move,
            "record:posted:%s" % stable_stamp,
            occurred_at=occurred_at,
            evidence_level=evidence_level,
            evidence_ref="record:%s" % move.id,
        )

    @api.model
    def _partial_event_key(self, partial_id, event_type):
        token = {
            "payment_allocated": "created",
            "payment_allocation_reversed": "removed",
        }.get(event_type)
        if not token:
            raise ValidationError(
                _("The payment-allocation event type is unsupported.")
            )
        return "account.partial.reconcile:%s:%s" % (partial_id, token)

    @api.model
    def _allocation_facts(self, partial):
        partial = self._lock_partial(partial)
        if not partial:
            return None
        candidates = []
        for invoice_line, payment_line in (
            (partial.debit_move_id, partial.credit_move_id),
            (partial.credit_move_id, partial.debit_move_id),
        ):
            invoice = invoice_line.move_id
            payment = payment_line.payment_id or payment_line.move_id.payment_id
            if (
                invoice.state != "posted"
                or invoice.move_type != "out_invoice"
                or invoice_line.account_id.account_type != "asset_receivable"
                or payment_line.account_id.account_type != "asset_receivable"
                or not payment
                or payment.state != "posted"
                or payment.payment_type != "inbound"
                or payment.partner_type != "customer"
                or payment.company_id != partial.company_id
            ):
                continue
            invoice_partner = invoice.partner_id.commercial_partner_id
            payment_partner = payment.partner_id.commercial_partner_id
            if not invoice_partner or invoice_partner != payment_partner:
                continue
            candidates.append((invoice, invoice_line, payment, payment_line))
        if len(candidates) != 1:
            return None
        invoice, invoice_line, payment, payment_line = candidates[0]
        currency = partial.company_currency_id
        amount = self._decimal_amount(currency, partial.amount)
        if amount <= 0:
            return None
        return {
            "amount": amount,
            "company": partial.company_id,
            "created_at": partial.create_date or fields.Datetime.now(),
            "currency": currency,
            "debit_line_id": partial.debit_move_id.id,
            "credit_line_id": partial.credit_move_id.id,
            "invoice": invoice,
            "invoice_line_id": invoice_line.id,
            "max_date": partial.max_date,
            "partial_id": partial.id,
            "payment": payment,
            "payment_entry": payment.move_id,
            "payment_line_id": payment_line.id,
        }

    @api.model
    def _allocation_extensions(self, facts):
        return {
            "account.allocation_basis": "partial_reconcile_company_currency",
            "account.company_currency": facts["currency"].name,
            "account.credit_line_id": facts["credit_line_id"],
            "account.debit_line_id": facts["debit_line_id"],
            "account.invoice_id": facts["invoice"].id,
            "account.invoice_line_id": facts["invoice_line_id"],
            "account.max_date": fields.Date.to_string(facts["max_date"]),
            "account.partial_reconcile_id": facts["partial_id"],
            "account.payment_id": facts["payment"].id,
            "account.payment_line_id": facts["payment_line_id"],
        }

    @api.model
    def _source_partial_event(self, company, partial_id, event_type):
        return (
            self.env["marketing.business.event"]
            .sudo()
            .search(
                [
                    ("company_id", "=", company.id),
                    ("source_system", "=", "odoo.account"),
                    ("source_model", "=", "account.partial.reconcile"),
                    ("source_res_id", "=", partial_id),
                    ("event_type", "=", event_type),
                    (
                        "business_event_key",
                        "=",
                        self._partial_event_key(partial_id, event_type),
                    ),
                ],
                limit=1,
            )
        )

    @api.model
    def _link_allocation_event(self, event, facts):
        self._link_event_move(event, facts["invoice"], "settled_invoice")
        self._link_event_move(event, facts["payment_entry"], "payment_entry")
        self._link_event_payment(event, facts["payment"])
        return event

    @api.model
    def _emit_allocation_event(
        self,
        facts,
        event_type,
        occurred_at=None,
        evidence_level="first_party",
        reversed_event=None,
    ):
        company = facts["company"]
        if company not in self.env.companies:
            raise AccessError(_("The payment allocation company is not available."))
        amount = facts["amount"]
        reverses_key = ""
        if event_type == "payment_allocation_reversed":
            reversed_event = (
                reversed_event.exists() if reversed_event else reversed_event
            )
            if (
                not reversed_event
                or len(reversed_event) != 1
                or reversed_event.event_type != "payment_allocated"
                or reversed_event.company_id != company
            ):
                raise ValidationError(
                    _(
                        "A payment-allocation reversal requires its exact allocation event."
                    )
                )
            amount = -(
                decimal.Decimal(reversed_event.amount_signed_micros)
                / decimal.Decimal(1_000_000)
            )
            reverses_key = reversed_event.business_event_key
        elif event_type != "payment_allocated":
            raise ValidationError(
                _("The payment-allocation event type is unsupported.")
            )
        partial_id = facts["partial_id"]
        occurrence_ref = (
            "removed" if event_type == "payment_allocation_reversed" else "created"
        )
        source_evidence_ref = "account.partial.reconcile:%s:%s:%s:%s" % (
            partial_id,
            occurrence_ref,
            facts["debit_line_id"],
            facts["credit_line_id"],
        )
        dto = MarketingBusinessEventDTO(
            event_class="revenue",
            event_type=event_type,
            source_system="odoo.account",
            source_model="account.partial.reconcile",
            source_res_id=partial_id,
            source_occurrence_ref=occurrence_ref,
            source_evidence_ref=source_evidence_ref,
            business_event_key=self._partial_event_key(partial_id, event_type),
            occurred_at=occurred_at or fields.Datetime.now(),
            evidence_level=evidence_level,
            amount_signed=amount,
            currency=facts["currency"].name,
            reverses_business_event_key=reverses_key,
            extensions=self._allocation_extensions(facts),
        )
        result = (
            self.env["marketing.business.event.service"]
            .with_company(company)
            ._ingest_event(company, dto)
        )
        event = self.env["marketing.business.event"].sudo().browse(result.event_id)
        return self._link_allocation_event(event, facts)

    @api.model
    def _ensure_allocation_event(self, partial, evidence_level="first_party"):
        facts = self._allocation_facts(partial)
        if not facts:
            return self.env["marketing.business.event"], None
        # Updating the claim while holding the partial row lock gives concurrent
        # first projectors a real MVCC conflict under Odoo's repeatable-read
        # transactions. The loser retries with a fresh snapshot and observes the
        # immutable event instead of racing into the unique index.
        self._claim_partial_event(partial)
        event = self._source_partial_event(
            facts["company"], facts["partial_id"], "payment_allocated"
        )
        if not event:
            event = self._emit_allocation_event(
                facts,
                "payment_allocated",
                occurred_at=facts["created_at"],
                evidence_level=evidence_level,
            )
        else:
            self._link_allocation_event(event, facts)
        return event, facts

    @api.model
    def _emit_allocation_reversal(self, event, facts):
        existing = self._source_partial_event(
            facts["company"], facts["partial_id"], "payment_allocation_reversed"
        )
        if existing:
            return self._link_allocation_event(existing, facts)
        return self._emit_allocation_event(
            facts,
            "payment_allocation_reversed",
            occurred_at=fields.Datetime.now(),
            reversed_event=event,
        )

    @api.model
    def _backfill_posted_moves(self, company, after_id=0, limit=200):
        company = company.exists() if company else company
        if (
            not company
            or getattr(company, "_name", "") != "res.company"
            or len(company) != 1
            or company not in self.env.companies
        ):
            raise AccessError(_("The accounting company is not available."))
        after_id = max(int(after_id or 0), 0)
        limit = min(max(int(limit or 200), 1), 1000)
        moves = (
            self.env["account.move"]
            .sudo()
            .with_company(company)
            .search(
                [
                    ("company_id", "=", company.id),
                    ("id", ">", after_id),
                    ("move_type", "in", tuple(OUTGOING_MOVE_TYPES)),
                    ("state", "=", "posted"),
                ],
                order="id asc",
                limit=limit,
            )
        )
        for move in moves:
            self._ensure_move_event(move, evidence_level="imported")
        last_id = moves[-1].id if moves else after_id
        has_more = bool(
            self.env["account.move"]
            .sudo()
            .search(
                [
                    ("company_id", "=", company.id),
                    ("id", ">", last_id),
                    ("move_type", "in", tuple(OUTGOING_MOVE_TYPES)),
                    ("state", "=", "posted"),
                ],
                limit=1,
            )
        )
        return {
            "processed": len(moves),
            "last_id": last_id,
            "has_more": has_more,
        }

    @api.model
    def _backfill_payment_allocations(self, company, after_id=0, limit=200):
        company = company.exists() if company else company
        if (
            not company
            or getattr(company, "_name", "") != "res.company"
            or len(company) != 1
            or company not in self.env.companies
        ):
            raise AccessError(_("The accounting company is not available."))
        after_id = max(int(after_id or 0), 0)
        limit = min(max(int(limit or 200), 1), 1000)
        partials = (
            self.env["account.partial.reconcile"]
            .sudo()
            .with_company(company)
            .search(
                [("company_id", "=", company.id), ("id", ">", after_id)],
                order="id asc",
                limit=limit,
            )
        )
        emitted = 0
        for partial in partials:
            event, _facts = self._ensure_allocation_event(
                partial, evidence_level="imported"
            )
            emitted += int(bool(event))
        last_id = partials[-1].id if partials else after_id
        has_more = bool(
            self.env["account.partial.reconcile"]
            .sudo()
            .search([("company_id", "=", company.id), ("id", ">", last_id)], limit=1)
        )
        return {
            "processed": len(partials),
            "emitted": emitted,
            "last_id": last_id,
            "has_more": has_more,
        }

    @api.model
    def _backfill_payment(self, payment, after_id=0, limit=200):
        # Keep the same lock order as live partial create/unlink: partial first,
        # payment second. Locking the payment here before `_allocation_facts`
        # would deadlock with a concurrent reconciliation removal.
        payment, company = self._payment_company_snapshot(payment)
        line_ids = payment.move_id.line_ids.ids
        after_id = max(int(after_id or 0), 0)
        limit = min(max(int(limit or 200), 1), 1000)
        partials = (
            self.env["account.partial.reconcile"]
            .sudo()
            .search(
                [
                    ("company_id", "=", company.id),
                    ("id", ">", after_id),
                    "|",
                    ("debit_move_id", "in", line_ids),
                    ("credit_move_id", "in", line_ids),
                ],
                order="id asc",
                limit=limit,
            )
        )
        emitted = 0
        for partial in partials:
            event, _facts = self._ensure_allocation_event(
                partial, evidence_level="imported"
            )
            emitted += int(bool(event))
        last_id = partials[-1].id if partials else after_id
        return {"processed": len(partials), "emitted": emitted, "last_id": last_id}

    @api.model
    def _move_action(self, moves):
        moves = moves.exists()
        moves.check_access_rights("read")
        moves.check_access_rule("read")
        action = {
            "type": "ir.actions.act_window",
            "name": _("Journal Entries"),
            "res_model": "account.move",
            "view_mode": "tree,form",
            "domain": [("id", "in", moves.ids)],
            "context": {"create": False},
        }
        if len(moves) == 1:
            action.update(
                {"res_id": moves.id, "view_mode": "form", "views": [(False, "form")]}
            )
        return action

    @api.model
    def _payment_action(self, payments):
        payments = payments.exists()
        payments.check_access_rights("read")
        payments.check_access_rule("read")
        action = {
            "type": "ir.actions.act_window",
            "name": _("Payments"),
            "res_model": "account.payment",
            "view_mode": "tree,form",
            "domain": [("id", "in", payments.ids)],
            "context": {"create": False},
        }
        if len(payments) == 1:
            action.update(
                {
                    "res_id": payments.id,
                    "view_mode": "form",
                    "views": [(False, "form")],
                }
            )
        return action

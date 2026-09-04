import decimal

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.marketing_center_base.services import MarketingBusinessEventDTO

from .tokens import MARKETING_SALE_INTERNAL_WRITE_TOKEN, MARKETING_SALE_LINK_WRITE_TOKEN

CONFIRMED_STATES = frozenset({"sale", "done"})


class MarketingSaleService(models.AbstractModel):
    _name = "marketing.sale.service"
    _description = "Marketing Sales Integration Service"

    @api.model
    def _company_for_order(self, order):
        order = order.exists()
        if getattr(order, "_name", "") != "sale.order" or len(order) != 1:
            raise ValidationError(_("A single valid sales order is required."))
        self.env.cr.execute(
            "SELECT id FROM sale_order WHERE id = %s FOR UPDATE", [order.id]
        )
        order.invalidate_recordset(["company_id", "marketing_sale_company_id"])
        company = order.marketing_sale_company_id or order.company_id
        if not company or company not in self.env.companies:
            raise AccessError(_("The sales order company is not available."))
        if order.company_id != company:
            raise ValidationError(
                _("The sales order company conflicts with its marketing scope.")
            )
        if not order.marketing_sale_company_id:
            order.with_context(
                marketing_sale_internal_write_token=MARKETING_SALE_INTERNAL_WRITE_TOKEN
            ).write({"marketing_sale_company_id": company.id})
        return company

    @api.model
    def _link_order_lead(self, order, lead, source_ref="sale.opportunity_id"):
        order = order.exists()
        lead = lead.exists()
        company = self._company_for_order(order)
        lead_company = self.env["marketing.crm.service"]._company_for_lead(lead)
        if lead_company != company:
            raise ValidationError(
                _("Sales orders and CRM leads cannot cross companies.")
            )
        source_ref = (source_ref or "sale.opportunity_id").strip()
        if not source_ref or len(source_ref) > 512:
            raise ValidationError(_("The sales-to-CRM link source is invalid."))
        lock_key = "marketing_sale_crm:%s:%s:%s" % (
            company.id,
            order.id,
            lead.id,
        )
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [lock_key]
        )
        Link = self.env["marketing.sale.order.crm.link"].sudo()
        link = Link.search(
            [
                ("company_id", "=", company.id),
                ("order_id", "=", order.id),
                ("lead_id", "=", lead.id),
            ],
            limit=1,
        )
        if not link:
            link = (
                Link.with_company(company)
                .with_context(
                    marketing_sale_link_write_token=MARKETING_SALE_LINK_WRITE_TOKEN
                )
                .create(
                    {
                        "company_id": company.id,
                        "order_id": order.id,
                        "lead_id": lead.id,
                        "source_ref": source_ref,
                    }
                )
            )
        # Keep the graph convergent when a lead is associated after an event was
        # recorded. Both ledgers stay immutable; only a missing immutable edge is
        # appended.
        event_links = (
            self.env["marketing.business.event.sale.link"]
            .sudo()
            .search([("company_id", "=", company.id), ("order_id", "=", order.id)])
        )
        crm_service = self.env["marketing.crm.service"]
        for event in event_links.mapped("event_id"):
            crm_service._link_event_lead(event, lead)
        return link

    @api.model
    def _link_event_order(self, event, order):
        event = event.exists()
        if getattr(event, "_name", "") != "marketing.business.event" or len(event) != 1:
            raise ValidationError(_("A single valid marketing event is required."))
        company = self._company_for_order(order)
        if event.company_id != company:
            raise ValidationError(
                _("Sales orders and marketing events cannot cross companies.")
            )
        lock_key = "marketing_sale_event:%s:%s:%s" % (
            company.id,
            order.id,
            event.id,
        )
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [lock_key]
        )
        Link = self.env["marketing.business.event.sale.link"].sudo()
        link = Link.search(
            [
                ("company_id", "=", company.id),
                ("order_id", "=", order.id),
                ("event_id", "=", event.id),
            ],
            limit=1,
        )
        if not link:
            link = (
                Link.with_company(company)
                .with_context(
                    marketing_sale_link_write_token=MARKETING_SALE_LINK_WRITE_TOKEN
                )
                .create(
                    {
                        "company_id": company.id,
                        "order_id": order.id,
                        "event_id": event.id,
                    }
                )
            )
        crm_service = self.env["marketing.crm.service"]
        lead_links = (
            self.env["marketing.sale.order.crm.link"]
            .sudo()
            .search([("company_id", "=", company.id), ("order_id", "=", order.id)])
        )
        for lead in lead_links.mapped("lead_id").exists():
            crm_service._link_event_lead(event, lead)
        return link

    @api.model
    def _event_key(self, order, event_type, occurrence_ref):
        event_token = {
            "proposal_sent": "proposal_sent",
            "order_confirmed": "confirmed",
            "order_cancelled": "cancelled",
        }.get(event_type)
        if not event_token:
            raise ValidationError(_("The sales event type is unsupported."))
        key = "sale.order:%s:%s:%s" % (order.id, event_token, occurrence_ref)
        if len(key) > 512:
            raise ValidationError(_("The sales event identity is too long."))
        return key

    @api.model
    def _existing_event(self, order, event_type, occurrence_ref):
        company = self._company_for_order(order)
        return (
            self.env["marketing.business.event"]
            .sudo()
            .search(
                [
                    ("company_id", "=", company.id),
                    ("source_system", "=", "odoo.sale"),
                    (
                        "business_event_key",
                        "=",
                        self._event_key(order, event_type, occurrence_ref),
                    ),
                ],
                limit=1,
            )
        )

    @api.model
    def _source_events(self, order, event_type=None):
        company = self._company_for_order(order)
        domain = [
            ("company_id", "=", company.id),
            ("source_system", "=", "odoo.sale"),
            ("source_model", "=", "sale.order"),
            ("source_res_id", "=", order.id),
        ]
        if event_type:
            domain.append(("event_type", "=", event_type))
        events = (
            self.env["marketing.business.event"]
            .sudo()
            .search(domain, order="occurred_at asc, id asc")
        )
        for event in events:
            self._link_event_order(event, order)
        return events

    @api.model
    def _amount_decimal(self, order):
        # Revenue/order-value reporting uses the untaxed commercial value. Gross
        # and tax projections remain explicit snapshot dimensions below, so a
        # future dashboard cannot accidentally mix both bases.
        rounded = order.currency_id.round(order.amount_untaxed)
        amount = decimal.Decimal(str(rounded)).quantize(decimal.Decimal("0.000001"))
        if amount < 0:
            raise ValidationError(
                _(
                    "A confirmed sales order needs a non-negative total to enter the "
                    "revenue event ledger."
                )
            )
        return amount

    @api.model
    def _amount_micros(self, order, field_name):
        rounded = order.currency_id.round(order[field_name])
        return int(
            decimal.Decimal(str(rounded)).quantize(decimal.Decimal("0.000001"))
            * decimal.Decimal(1_000_000)
        )

    @api.model
    def _extensions(self, order, old_state, new_state, sequence=0):
        values = {
            "sale.order_name": order.name,
            "sale.partner_id": order.partner_id.id,
            "sale.old_state": old_state or "unknown",
            "sale.new_state": new_state or order.state,
            "sale.amount_basis": "untaxed",
            "sale.amount_untaxed_micros": self._amount_micros(order, "amount_untaxed"),
            "sale.amount_tax_micros": self._amount_micros(order, "amount_tax"),
            "sale.amount_total_micros": self._amount_micros(order, "amount_total"),
            "sale.currency": order.currency_id.name,
            "sale.team_id": order.team_id.id or 0,
            "sale.user_id": order.user_id.id or 0,
        }
        if order.opportunity_id:
            values["sale.opportunity_id"] = order.opportunity_id.id
        for field_name in ("campaign_id", "medium_id", "source_id"):
            if field_name in order._fields and order[field_name]:
                values["sale.%s" % field_name] = order[field_name].id
        if sequence:
            values["sale.transition_sequence"] = sequence
        return values

    @api.model
    def _emit_order_event(
        self,
        order,
        event_type,
        occurrence_ref,
        occurred_at=None,
        old_state="",
        new_state="",
        sequence=0,
        evidence_level="first_party",
        reversed_event=None,
        evidence_ref="",
    ):
        company = self._company_for_order(order)
        occurrence_ref = (occurrence_ref or "").strip()
        if not occurrence_ref or len(occurrence_ref) > 512:
            raise ValidationError(_("The sales event occurrence reference is invalid."))
        source_evidence_ref = "sale.order:%s:%s" % (
            order.id,
            evidence_ref or occurrence_ref,
        )
        if len(source_evidence_ref) > 512:
            raise ValidationError(_("The sales event evidence reference is too long."))
        event_class = "lifecycle" if event_type == "proposal_sent" else "revenue"
        amount = None
        currency = ""
        reverses_key = ""
        if event_type == "order_confirmed":
            amount = self._amount_decimal(order)
            currency = order.currency_id.name
        elif event_type == "order_cancelled":
            reversed_event = (
                reversed_event.exists() if reversed_event else reversed_event
            )
            if (
                not reversed_event
                or len(reversed_event) != 1
                or reversed_event.event_type != "order_confirmed"
                or reversed_event.company_id != company
            ):
                raise ValidationError(
                    _("An order cancellation requires its exact confirmation event.")
                )
            amount = -(
                decimal.Decimal(reversed_event.amount_signed_micros)
                / decimal.Decimal(1_000_000)
            )
            currency = reversed_event.currency_id.name
            reverses_key = reversed_event.business_event_key
        dto = MarketingBusinessEventDTO(
            event_class=event_class,
            event_type=event_type,
            source_system="odoo.sale",
            source_model="sale.order",
            source_res_id=order.id,
            source_occurrence_ref=occurrence_ref,
            source_evidence_ref=source_evidence_ref,
            business_event_key=self._event_key(order, event_type, occurrence_ref),
            occurred_at=occurred_at or fields.Datetime.now(),
            evidence_level=evidence_level,
            amount_signed=amount,
            currency=currency,
            reverses_business_event_key=reverses_key,
            extensions=self._extensions(order, old_state, new_state, sequence=sequence),
        )
        result = (
            self.env["marketing.business.event.service"]
            .with_company(company)
            ._ingest_event(company, dto)
        )
        event = self.env["marketing.business.event"].sudo().browse(result.event_id)
        self._link_event_order(event, order)
        return event

    @api.model
    def _first_proposal_event(self, order):
        return self._source_events(order, "proposal_sent")[:1]

    @api.model
    def _open_confirmation(self, order):
        confirmations = self._source_events(order, "order_confirmed")
        if not confirmations:
            return confirmations
        reversed_ids = set(
            self.env["marketing.business.event"]
            .sudo()
            .search(
                [
                    ("company_id", "=", order.company_id.id),
                    ("event_type", "=", "order_cancelled"),
                    ("reverses_event_id", "in", confirmations.ids),
                ]
            )
            .mapped("reverses_event_id")
            .ids
        )
        active = confirmations.filtered(lambda event: event.id not in reversed_ids)
        if len(active) > 1:
            raise ValidationError(
                _(
                    "The sales order has multiple unreversed confirmations. "
                    "Reconcile the event ledger before cancelling it."
                )
            )
        return active

    @api.model
    def _ensure_confirmation(self, order, evidence_level="imported"):
        confirmation = self._open_confirmation(order)
        if confirmation:
            return confirmation
        occurred_at = order.date_order or order.create_date or fields.Datetime.now()
        stable_stamp = fields.Datetime.to_string(occurred_at).replace(" ", "T")
        occurrence_ref = "record:date_order:%s" % stable_stamp
        return self._emit_order_event(
            order,
            "order_confirmed",
            occurrence_ref,
            occurred_at=occurred_at,
            old_state="unknown",
            new_state="sale",
            evidence_level=evidence_level,
        )

    @api.model
    def _tracking_state_map(self):
        mapping = {
            "draft": "draft",
            "sent": "sent",
            "sale": "sale",
            "done": "done",
            "cancel": "cancel",
            "quotation": "draft",
            "quotation sent": "sent",
            "sales order": "sale",
            "locked": "done",
            "cancelled": "cancel",
        }
        languages = (
            self.env["res.lang"].sudo().with_context(active_test=False).search([])
        )
        Order = self.env["sale.order"]
        for language in languages:
            selection = (
                Order.with_context(lang=language.code)
                .fields_get(["state"])["state"]
                .get("selection", [])
            )
            for key, label in selection:
                mapping[(label or "").strip().casefold()] = key
        return mapping

    @api.model
    def _tracking_state(self, value, mapping):
        return mapping.get((value or "").strip().casefold(), "")

    @api.model
    def _backfill_order_events(self, order, after_tracking_id=0, limit=200):
        company = self._company_for_order(order)
        if order.opportunity_id:
            self._link_order_lead(order, order.opportunity_id, "sale.backfill")
        after_tracking_id = max(int(after_tracking_id or 0), 0)
        limit = min(max(int(limit or 200), 1), 1000)
        Tracking = self.env["mail.tracking.value"].sudo()
        tracking_domain = [
            ("id", ">", after_tracking_id),
            ("mail_message_id.model", "=", "sale.order"),
            ("mail_message_id.res_id", "=", order.id),
            ("field.name", "=", "state"),
        ]
        tracking_values = Tracking.search(
            tracking_domain,
            order="id asc",
            limit=limit,
        )
        state_map = self._tracking_state_map()
        for tracking in tracking_values:
            old_state = self._tracking_state(tracking.old_value_char, state_map)
            new_state = self._tracking_state(tracking.new_value_char, state_map)
            if not old_state or not new_state or old_state == new_state:
                continue
            occurrence_ref = "tracking:%s" % tracking.id
            occurred_at = tracking.mail_message_id.date or order.write_date
            # Odoo may deliberately omit the draft -> sent tracking row when the
            # quotation is sent by mail.  A later sent -> sale transition is still
            # first-party evidence that the proposal had been sent, so project the
            # missing lifecycle event before the confirmation from that same row.
            if (
                new_state == "sent" or old_state == "sent"
            ) and not self._first_proposal_event(order):
                proposal_ref = occurrence_ref
                proposal_old_state = old_state
                if old_state == "sent":
                    proposal_ref = "%s:observed-old-state:sent" % occurrence_ref
                    proposal_old_state = "unknown"
                self._emit_order_event(
                    order,
                    "proposal_sent",
                    proposal_ref,
                    occurred_at=occurred_at,
                    old_state=proposal_old_state,
                    new_state="sent",
                    evidence_level="imported",
                    evidence_ref=occurrence_ref,
                )
            if old_state not in CONFIRMED_STATES and new_state in CONFIRMED_STATES:
                if not self._existing_event(order, "order_confirmed", occurrence_ref):
                    self._emit_order_event(
                        order,
                        "order_confirmed",
                        occurrence_ref,
                        occurred_at=occurred_at,
                        old_state=old_state,
                        new_state=new_state,
                        evidence_level="imported",
                    )
            elif old_state in CONFIRMED_STATES and new_state == "cancel":
                if not self._existing_event(order, "order_cancelled", occurrence_ref):
                    confirmation = self._open_confirmation(order)
                    if not confirmation:
                        confirmation = self._ensure_confirmation(order)
                    self._emit_order_event(
                        order,
                        "order_cancelled",
                        occurrence_ref,
                        occurred_at=occurred_at,
                        old_state=old_state,
                        new_state=new_state,
                        evidence_level="imported",
                        reversed_event=confirmation,
                    )

        last_tracking_id = tracking_values[-1:].id or after_tracking_id
        has_more = bool(
            Tracking.search(
                [
                    ("id", ">", last_tracking_id),
                    ("mail_message_id.model", "=", "sale.order"),
                    ("mail_message_id.res_id", "=", order.id),
                    ("field.name", "=", "state"),
                ],
                limit=1,
            )
        )
        if not has_more:
            self._backfill_current_state(order)
        return {
            "processed": len(tracking_values),
            "last_tracking_id": last_tracking_id,
            "has_more": has_more,
            "company_id": company.id,
        }

    @api.model
    def _backfill_current_state(self, order):
        occurred_at = order.write_date or order.create_date or fields.Datetime.now()
        stable_stamp = fields.Datetime.to_string(occurred_at).replace(" ", "T")
        if order.state == "sent" and not self._first_proposal_event(order):
            self._emit_order_event(
                order,
                "proposal_sent",
                "record:state:sent:%s" % stable_stamp,
                occurred_at=occurred_at,
                old_state="draft",
                new_state="sent",
                evidence_level="imported",
            )
        if order.state in CONFIRMED_STATES and not self._open_confirmation(order):
            self._ensure_confirmation(order)
        if order.state == "cancel":
            confirmation = self._open_confirmation(order)
            if confirmation:
                occurrence_ref = "record:state:cancel:%s" % stable_stamp
                if not self._existing_event(order, "order_cancelled", occurrence_ref):
                    self._emit_order_event(
                        order,
                        "order_cancelled",
                        occurrence_ref,
                        occurred_at=occurred_at,
                        old_state="sale",
                        new_state="cancel",
                        evidence_level="imported",
                        reversed_event=confirmation,
                    )

    @api.model
    def _order_action(self, orders):
        orders = orders.exists()
        orders.check_access_rights("read")
        orders.check_access_rule("read")
        action = {
            "type": "ir.actions.act_window",
            "name": _("Sales Orders"),
            "res_model": "sale.order",
            "view_mode": "tree,form",
            "domain": [("id", "in", orders.ids)],
            "context": {"create": False},
        }
        if len(orders) == 1:
            action.update(
                {"res_id": orders.id, "view_mode": "form", "views": [(False, "form")]}
            )
        return action

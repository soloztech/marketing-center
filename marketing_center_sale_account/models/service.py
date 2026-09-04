from odoo import _, api, models
from odoo.exceptions import AccessError, ValidationError

from .tokens import MARKETING_SALE_ACCOUNT_LINK_WRITE_TOKEN

OUTGOING_MOVE_TYPES = frozenset({"out_invoice", "out_refund"})
TYPED_SALE_SOURCE_REF = "account.move.invoice_line_ids.sale_line_ids"


class MarketingAccountService(models.AbstractModel):
    _inherit = "marketing.account.service"

    @api.model
    def _claim_sale_account_projection(self, move, account_link):
        """Create an MVCC conflict before a first typed Sales projection."""
        self.env.cr.execute(
            "SELECT id "
            "FROM marketing_business_event_account_move_link "
            "WHERE id = %s FOR UPDATE",
            [account_link.id],
        )
        if not self.env.cr.fetchone():
            raise ValidationError(
                _("The accounting event link is no longer available.")
            )
        account_link.invalidate_recordset(["marketing_sale_projection_claimed"])
        if account_link.marketing_sale_projection_claimed:
            return account_link

        # The move row is already locked by `_validate_projection_source`. A
        # physical no-op update creates a new MVCC version, so another first
        # projector that began from a stale repeatable-read snapshot receives a
        # retryable SerializationFailure before it can race either unique key.
        self.env.cr.execute(
            "UPDATE account_move SET write_date = write_date WHERE id = %s",
            [move.id],
        )
        self.env.cr.execute(
            "UPDATE marketing_business_event_account_move_link "
            "SET marketing_sale_projection_claimed = TRUE WHERE id = %s",
            [account_link.id],
        )
        account_link.invalidate_recordset(["marketing_sale_projection_claimed"])
        return account_link

    @api.model
    def _typed_invoice_orders(self, move):
        move = move.exists()
        if getattr(move, "_name", "") != "account.move" or len(move) != 1:
            raise ValidationError(_("A single valid journal entry is required."))
        if move.move_type not in OUTGOING_MOVE_TYPES:
            return self.env["sale.order"]
        sale_lines = move.invoice_line_ids.mapped("sale_line_ids")
        orders = sale_lines.mapped("order_id").exists()
        if orders.filtered(lambda order: order.company_id != move.company_id):
            raise ValidationError(
                _("Typed invoice sales orders cannot cross companies.")
            )
        return orders.sorted("id")

    @api.model
    def _link_typed_move_order(self, move, order):
        company = self._company_for_move(move)
        order_company = self.env["marketing.sale.service"]._company_for_order(order)
        if order_company != company:
            raise ValidationError(
                _("Journal entries and sales orders cannot cross companies.")
            )
        lock_key = "marketing_sale_account:%s:%s:%s" % (
            company.id,
            move.id,
            order.id,
        )
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [lock_key]
        )
        Link = self.env["marketing.account.move.sale.link"].sudo()
        link = Link.search(
            [
                ("company_id", "=", company.id),
                ("move_id", "=", move.id),
                ("order_id", "=", order.id),
            ],
            limit=1,
        )
        if not link:
            link = (
                Link.with_company(company)
                .with_context(
                    marketing_sale_account_link_write_token=(
                        MARKETING_SALE_ACCOUNT_LINK_WRITE_TOKEN
                    )
                )
                .create(
                    {
                        "company_id": company.id,
                        "move_id": move.id,
                        "order_id": order.id,
                        "source_ref": TYPED_SALE_SOURCE_REF,
                    }
                )
            )
        return link

    @api.model
    def _validate_projection_source(self, event, move, role, account_link):
        event = event.exists()
        move = move.exists()
        account_link = account_link.exists()
        if getattr(event, "_name", "") != "marketing.business.event" or len(event) != 1:
            raise ValidationError(_("A single valid marketing event is required."))
        if getattr(move, "_name", "") != "account.move" or len(move) != 1:
            raise ValidationError(_("A single valid journal entry is required."))
        if (
            getattr(account_link, "_name", "")
            != "marketing.business.event.account.move.link"
            or len(account_link) != 1
        ):
            raise ValidationError(
                _("A single valid accounting event link is required.")
            )
        company = self._company_for_move(move)
        if (
            account_link.company_id != company
            or account_link.event_id != event
            or account_link.move_id != move
            or account_link.role != role
            or event.company_id != company
        ):
            raise ValidationError(
                _("The accounting event link does not match its projection source.")
            )
        return event, move, account_link, company

    @api.model
    def _project_event_move_sale_links(self, event, move, role, account_link):
        event, move, account_link, company = self._validate_projection_source(
            event, move, role, account_link
        )
        self._claim_sale_account_projection(move, account_link)
        lock_key = "marketing_sale_account_projection:%s:%s" % (
            company.id,
            account_link.id,
        )
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [lock_key]
        )
        Projection = self.env["marketing.sale.account.projection"].sudo()
        projection = Projection.search(
            [("account_link_id", "=", account_link.id)], limit=1
        )
        if projection:
            return projection, False

        orders = self._typed_invoice_orders(move)
        sale_service = self.env["marketing.sale.service"]
        for order in orders:
            self._link_typed_move_order(move, order)
            sale_service._link_event_order(event, order)
        projection = (
            Projection.with_company(company)
            .with_context(
                marketing_sale_account_link_write_token=(
                    MARKETING_SALE_ACCOUNT_LINK_WRITE_TOKEN
                )
            )
            .create(
                {
                    "account_link_id": account_link.id,
                    "source_order_count": len(orders),
                }
            )
        )
        return projection, True

    @api.model
    def _after_link_event_move(self, event, move, role, account_link, created):
        result = super()._after_link_event_move(
            event, move, role, account_link, created
        )
        # The receipt, rather than `created`, is authoritative. This lets a
        # bounded backfill project account links that predate this bridge while
        # keeping every subsequent replay frozen.
        self._project_event_move_sale_links(event, move, role, account_link)
        return result

    @api.model
    def _backfill_sale_account_projections(self, company, after_id=0, limit=500):
        company = company.exists() if company else company
        if (
            not company
            or getattr(company, "_name", "") != "res.company"
            or len(company) != 1
        ):
            raise ValidationError(_("A single valid company is required."))
        if company not in self.env.companies:
            raise AccessError(_("The company is not available for this backfill."))
        if isinstance(after_id, bool) or not isinstance(after_id, int) or after_id < 0:
            raise ValidationError(
                _("The backfill cursor must be a non-negative integer.")
            )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise ValidationError(
                _("The backfill page size must be between 1 and 1000.")
            )

        links = (
            self.env["marketing.business.event.account.move.link"]
            .sudo()
            .with_company(company)
            .search(
                [("company_id", "=", company.id), ("id", ">", after_id)],
                order="id asc",
                limit=limit,
            )
        )
        projected = 0
        skipped_missing_source = 0
        for account_link in links:
            move = account_link.move_id.exists()
            if not move:
                # The immutable accounting evidence can legitimately outlive the
                # journal entry. There is no longer a typed invoice graph to
                # project, so advance the cursor without fabricating causality.
                skipped_missing_source += 1
                continue
            _projection, created = self._project_event_move_sale_links(
                account_link.event_id,
                move,
                account_link.role,
                account_link,
            )
            projected += int(created)
        last_id = links[-1].id if links else after_id
        has_more = bool(
            self.env["marketing.business.event.account.move.link"]
            .sudo()
            .with_company(company)
            .search(
                [("company_id", "=", company.id), ("id", ">", last_id)],
                limit=1,
            )
        )
        return {
            "processed": len(links),
            "projected": projected,
            "skipped_missing_source": skipped_missing_source,
            "last_id": last_id,
            "has_more": has_more,
        }

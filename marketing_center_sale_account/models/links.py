from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .tokens import MARKETING_SALE_ACCOUNT_LINK_WRITE_TOKEN


class ImmutableMarketingSaleAccountLinkMixin(models.AbstractModel):
    _name = "marketing.sale.account.link.immutable.mixin"
    _description = "Immutable Marketing Sales Accounting Link"

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("marketing_sale_account_link_write_token")
            is not MARKETING_SALE_ACCOUNT_LINK_WRITE_TOKEN
        ):
            raise AccessError(
                _(
                    "Marketing sales-accounting links are created only by their "
                    "service."
                )
            )
        return super().create(vals_list)

    def write(self, values):  # pylint: disable=method-required-super
        raise AccessError(_("Marketing sales-accounting links cannot be edited."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(  # pylint: disable=no-raise-unlink
            _("Marketing sales-accounting links cannot be deleted.")
        )


class MarketingBusinessEventAccountMoveLink(models.Model):
    _inherit = "marketing.business.event.account.move.link"

    marketing_sale_projection_claimed = fields.Boolean(
        string="Sales projection claimed",
        default=False,
        readonly=True,
        copy=False,
        help=(
            "Internal transaction claim used to serialize the first typed Sales "
            "projection of this accounting event link."
        ),
    )


class MarketingAccountMoveSaleLink(models.Model):
    _name = "marketing.account.move.sale.link"
    _description = "Marketing Invoice Sales Order Link"
    _inherit = "marketing.sale.account.link.immutable.mixin"
    _order = "linked_at desc, id desc"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict", readonly=True
    )

    @api.model_create_multi
    def create(self, vals_list):
        normalized = []
        for original in vals_list:
            values = dict(original)
            move = self.env["account.move"].browse(values.get("move_id")).exists()
            order = self.env["sale.order"].browse(values.get("order_id")).exists()
            if len(move) != 1 or len(order) != 1:
                raise ValidationError(
                    _(
                        "Live journal-entry and sales-order records are required "
                        "to append this typed link."
                    )
                )
            values.update(
                {
                    "move_model": "account.move",
                    "move_res_id": move.id,
                    "move_ref": (move.name or "account.move#%s" % move.id)[:256],
                    "order_model": "sale.order",
                    "order_res_id": order.id,
                    "order_ref": (order.name or "sale.order#%s" % order.id)[:256],
                }
            )
            normalized.append(values)
        return super().create(normalized)

    move_id = fields.Many2one(
        "account.move",
        index=True,
        ondelete="set null",
        check_company=True,
        readonly=True,
    )
    move_model = fields.Char(
        required=True, size=128, default="account.move", readonly=True
    )
    move_res_id = fields.Integer(required=True, index=True, readonly=True)
    move_ref = fields.Char(required=True, size=256, readonly=True)
    order_id = fields.Many2one(
        "sale.order",
        index=True,
        ondelete="set null",
        check_company=True,
        readonly=True,
    )
    order_model = fields.Char(
        required=True, size=128, default="sale.order", readonly=True
    )
    order_res_id = fields.Integer(required=True, index=True, readonly=True)
    order_ref = fields.Char(required=True, size=256, readonly=True)
    source_ref = fields.Char(required=True, size=512, index=True, readonly=True)
    linked_at = fields.Datetime(
        required=True, default=fields.Datetime.now, index=True, readonly=True
    )

    _sql_constraints = [
        (
            "move_order_unique",
            "unique(company_id, move_model, move_res_id, order_model, order_res_id)",
            "This journal entry is already linked to this sales order.",
        ),
        (
            "move_res_id_positive",
            "check(move_res_id > 0)",
            "The original journal-entry identifier must be positive.",
        ),
        (
            "order_res_id_positive",
            "check(order_res_id > 0)",
            "The original sales-order identifier must be positive.",
        ),
    ]

    @api.constrains("company_id", "move_id", "order_id")
    def _check_company_scope(self):
        for link in self:
            if (link.move_id and link.move_id.company_id != link.company_id) or (
                link.order_id and link.order_id.company_id != link.company_id
            ):
                raise ValidationError(
                    _("Journal entries and sales orders cannot cross companies.")
                )


class MarketingSaleAccountProjection(models.Model):
    _name = "marketing.sale.account.projection"
    _description = "Marketing Sales Accounting Projection Receipt"
    _inherit = "marketing.sale.account.link.immutable.mixin"
    _order = "projected_at desc, id desc"

    account_link_id = fields.Many2one(
        "marketing.business.event.account.move.link",
        required=True,
        index=True,
        ondelete="restrict",
        readonly=True,
    )
    company_id = fields.Many2one(
        related="account_link_id.company_id", store=True, index=True, readonly=True
    )
    event_id = fields.Many2one(
        related="account_link_id.event_id", store=True, index=True, readonly=True
    )
    move_id = fields.Many2one(
        related="account_link_id.move_id",
        store=True,
        index=True,
        readonly=True,
        ondelete="set null",
    )
    role = fields.Selection(
        related="account_link_id.role", store=True, index=True, readonly=True
    )
    source_order_count = fields.Integer(required=True, readonly=True)
    projected_at = fields.Datetime(
        required=True, default=fields.Datetime.now, index=True, readonly=True
    )

    _sql_constraints = [
        (
            "account_link_unique",
            "unique(account_link_id)",
            "This accounting role has already been projected to Sales.",
        ),
        (
            "source_order_count_non_negative",
            "check(source_order_count >= 0)",
            "The projected sales-order count cannot be negative.",
        ),
    ]


class AccountMove(models.Model):
    _inherit = "account.move"

    marketing_account_sale_link_ids = fields.One2many(
        "marketing.account.move.sale.link",
        "move_id",
        readonly=True,
        copy=False,
        groups=(
            "account.group_account_invoice,"
            "marketing_center_base.group_marketing_center_analyst"
        ),
    )
    marketing_sale_order_count = fields.Integer(
        compute="_compute_marketing_sale_order_count",
        groups=(
            "account.group_account_invoice,"
            "marketing_center_base.group_marketing_center_analyst"
        ),
    )

    def _compute_marketing_sale_order_count(self):
        grouped = self.env["marketing.account.move.sale.link"].read_group(
            [("move_id", "in", self.ids)], ["move_id"], ["move_id"]
        )
        counts = {row["move_id"][0]: row["move_id_count"] for row in grouped}
        for move in self:
            move.marketing_sale_order_count = counts.get(move.id, 0)

    def action_view_marketing_sale_orders(self):
        self.ensure_one()
        self.check_access_rights("read")
        self.check_access_rule("read")
        orders = self.marketing_account_sale_link_ids.mapped("order_id").exists()
        return self.env["marketing.sale.service"]._order_action(orders)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    marketing_account_move_link_ids = fields.One2many(
        "marketing.account.move.sale.link",
        "order_id",
        readonly=True,
        copy=False,
        groups="account.group_account_invoice",
    )
    marketing_account_move_count = fields.Integer(
        compute="_compute_marketing_account_move_count",
        groups="account.group_account_invoice",
    )

    def _compute_marketing_account_move_count(self):
        grouped = self.env["marketing.account.move.sale.link"].read_group(
            [("order_id", "in", self.ids)], ["order_id"], ["order_id"]
        )
        counts = {row["order_id"][0]: row["order_id_count"] for row in grouped}
        for order in self:
            order.marketing_account_move_count = counts.get(order.id, 0)

    def action_view_marketing_account_moves(self):
        self.ensure_one()
        self.check_access_rights("read")
        self.check_access_rule("read")
        moves = self.marketing_account_move_link_ids.mapped("move_id").exists()
        return self.env["marketing.account.service"]._move_action(moves)

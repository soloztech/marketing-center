from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .tokens import MARKETING_SALE_LINK_WRITE_TOKEN


class ImmutableMarketingSaleLinkMixin(models.AbstractModel):
    _name = "marketing.sale.link.immutable.mixin"
    _description = "Immutable Marketing Sales Link"

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("marketing_sale_link_write_token")
            is not MARKETING_SALE_LINK_WRITE_TOKEN
        ):
            raise AccessError(
                _("Marketing sales links are created only by their service.")
            )
        normalized = []
        for original in vals_list:
            values = dict(original)
            if "order_id" in self._fields:
                order = self.env["sale.order"].browse(values.get("order_id")).exists()
                if len(order) != 1:
                    raise ValidationError(
                        _("A live sales order is required to append this link.")
                    )
                values.update(
                    {
                        "order_model": "sale.order",
                        "order_res_id": order.id,
                        "order_ref": (order.name or "sale.order#%s" % order.id)[:256],
                    }
                )
            if "lead_id" in self._fields:
                lead = self.env["crm.lead"].browse(values.get("lead_id")).exists()
                if len(lead) != 1:
                    raise ValidationError(
                        _("A live CRM lead is required to append this link.")
                    )
                values.update(
                    {
                        "lead_model": "crm.lead",
                        "lead_res_id": lead.id,
                        "lead_ref": (lead.name or "crm.lead#%s" % lead.id)[:256],
                    }
                )
            normalized.append(values)
        return super().create(normalized)

    def write(self, values):  # pylint: disable=method-required-super
        raise AccessError(_("Marketing sales links cannot be edited."))

    def unlink(self):  # pylint: disable=method-required-super
        # The model is an append-only evidence edge by design.
        raise AccessError(  # pylint: disable=no-raise-unlink
            _("Marketing sales links cannot be deleted.")
        )


class MarketingSaleOrderCrmLink(models.Model):
    _name = "marketing.sale.order.crm.link"
    _description = "Marketing Sales Order CRM Link"
    _inherit = "marketing.sale.link.immutable.mixin"
    _order = "linked_at desc, id desc"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict", readonly=True
    )
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
    lead_id = fields.Many2one(
        "crm.lead",
        index=True,
        ondelete="set null",
        check_company=True,
        readonly=True,
    )
    lead_model = fields.Char(required=True, size=128, default="crm.lead", readonly=True)
    lead_res_id = fields.Integer(required=True, index=True, readonly=True)
    lead_ref = fields.Char(required=True, size=256, readonly=True)
    source_ref = fields.Char(required=True, size=512, index=True, readonly=True)
    linked_at = fields.Datetime(
        required=True, default=fields.Datetime.now, index=True, readonly=True
    )

    _sql_constraints = [
        (
            "order_lead_unique",
            "unique(company_id, order_model, order_res_id, lead_model, lead_res_id)",
            "This sales order is already linked to this CRM lead.",
        ),
        (
            "order_res_id_positive",
            "check(order_res_id > 0)",
            "The original sales-order identifier must be positive.",
        ),
        (
            "lead_res_id_positive",
            "check(lead_res_id > 0)",
            "The original CRM lead identifier must be positive.",
        ),
    ]

    @api.constrains("company_id", "order_id", "lead_id")
    def _check_company_scope(self):
        for link in self:
            lead_company = link.lead_id and (
                link.lead_id.marketing_event_company_id or link.lead_id.company_id
            )
            if (link.order_id and link.order_id.company_id != link.company_id) or (
                lead_company and lead_company != link.company_id
            ):
                raise ValidationError(
                    _("Sales orders and CRM leads cannot cross companies.")
                )


class MarketingBusinessEventSaleLink(models.Model):
    _name = "marketing.business.event.sale.link"
    _description = "Marketing Business Event Sales Order Link"
    _inherit = "marketing.sale.link.immutable.mixin"
    _order = "linked_at desc, id desc"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict", readonly=True
    )
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
    event_id = fields.Many2one(
        "marketing.business.event",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    linked_at = fields.Datetime(
        required=True, default=fields.Datetime.now, index=True, readonly=True
    )

    _sql_constraints = [
        (
            "order_event_unique",
            "unique(company_id, order_model, order_res_id, event_id)",
            "This marketing event is already linked to this sales order.",
        ),
        (
            "order_res_id_positive",
            "check(order_res_id > 0)",
            "The original sales-order identifier must be positive.",
        ),
    ]

    @api.constrains("company_id", "order_id", "event_id")
    def _check_company_scope(self):
        for link in self:
            if (
                link.order_id and link.order_id.company_id != link.company_id
            ) or link.event_id.company_id != link.company_id:
                raise ValidationError(
                    _("Sales orders and marketing events cannot cross companies.")
                )


class CrmLead(models.Model):
    _inherit = "crm.lead"

    marketing_sale_order_link_ids = fields.One2many(
        "marketing.sale.order.crm.link", "lead_id", readonly=True
    )
    marketing_sale_order_count = fields.Integer(
        compute="_compute_marketing_sale_order_count"
    )

    def _compute_marketing_sale_order_count(self):
        grouped = self.env["marketing.sale.order.crm.link"].read_group(
            [("lead_id", "in", self.ids), ("order_id", "!=", False)],
            ["lead_id"],
            ["lead_id"],
        )
        counts = {row["lead_id"][0]: row["lead_id_count"] for row in grouped}
        for lead in self:
            lead.marketing_sale_order_count = counts.get(lead.id, 0)

    def action_view_marketing_sale_orders(self):
        self.ensure_one()
        orders = self.marketing_sale_order_link_ids.mapped("order_id").exists()
        return self.env["marketing.sale.service"]._order_action(orders)


class MarketingBusinessEvent(models.Model):
    _inherit = "marketing.business.event"

    sale_order_link_ids = fields.One2many(
        "marketing.business.event.sale.link", "event_id", readonly=True
    )
    sale_order_count = fields.Integer(compute="_compute_sale_order_count")

    def _compute_sale_order_count(self):
        grouped = self.env["marketing.business.event.sale.link"].read_group(
            [("event_id", "in", self.ids), ("order_id", "!=", False)],
            ["event_id"],
            ["event_id"],
        )
        counts = {row["event_id"][0]: row["event_id_count"] for row in grouped}
        for event in self:
            event.sale_order_count = counts.get(event.id, 0)

    def action_view_sale_orders(self):
        self.ensure_one()
        self.check_access_rights("read")
        self.check_access_rule("read")
        orders = self.sale_order_link_ids.mapped("order_id").exists()
        return self.env["marketing.sale.service"]._order_action(orders)

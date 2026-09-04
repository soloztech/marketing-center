from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .tokens import MARKETING_ACCOUNT_LINK_WRITE_TOKEN


class ImmutableMarketingAccountLinkMixin(models.AbstractModel):
    _name = "marketing.account.link.immutable.mixin"
    _description = "Immutable Marketing Accounting Link"

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("marketing_account_link_write_token")
            is not MARKETING_ACCOUNT_LINK_WRITE_TOKEN
        ):
            raise AccessError(
                _("Marketing accounting links are created only by their service.")
            )
        normalized = []
        for original in vals_list:
            values = dict(original)
            if "move_id" in self._fields:
                move = self.env["account.move"].browse(values.get("move_id")).exists()
                if len(move) != 1:
                    raise ValidationError(
                        _("A live journal entry is required to append this link.")
                    )
                values.update(
                    {
                        "move_model": "account.move",
                        "move_res_id": move.id,
                        "move_ref": (move.name or "account.move#%s" % move.id)[:256],
                    }
                )
            if "payment_id" in self._fields:
                payment = (
                    self.env["account.payment"]
                    .browse(values.get("payment_id"))
                    .exists()
                )
                if len(payment) != 1:
                    raise ValidationError(
                        _("A live payment is required to append this link.")
                    )
                payment_ref = payment.move_id.name or "account.payment#%s" % payment.id
                values.update(
                    {
                        "payment_model": "account.payment",
                        "payment_res_id": payment.id,
                        "payment_ref": payment_ref[:256],
                    }
                )
            normalized.append(values)
        return super().create(normalized)

    def write(self, values):  # pylint: disable=method-required-super
        raise AccessError(_("Marketing accounting links cannot be edited."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(  # pylint: disable=no-raise-unlink
            _("Marketing accounting links cannot be deleted.")
        )


class MarketingBusinessEventAccountMoveLink(models.Model):
    _name = "marketing.business.event.account.move.link"
    _description = "Marketing Business Event Journal Entry Link"
    _inherit = "marketing.account.link.immutable.mixin"
    _order = "linked_at desc, id desc"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict", readonly=True
    )
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
    event_id = fields.Many2one(
        "marketing.business.event",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    role = fields.Selection(
        [
            ("source", "Source document"),
            ("reversed_invoice", "Reversed invoice"),
            ("settled_invoice", "Settled invoice"),
            ("payment_entry", "Payment journal entry"),
        ],
        required=True,
        index=True,
        readonly=True,
    )
    linked_at = fields.Datetime(
        required=True, default=fields.Datetime.now, index=True, readonly=True
    )

    _sql_constraints = [
        (
            "move_event_role_unique",
            "unique(company_id, move_model, move_res_id, event_id, role)",
            "This marketing event already has this journal-entry role.",
        ),
        (
            "move_res_id_positive",
            "check(move_res_id > 0)",
            "The original journal-entry identifier must be positive.",
        ),
    ]

    @api.constrains("company_id", "move_id", "event_id")
    def _check_company_scope(self):
        for link in self:
            if (
                link.move_id and link.move_id.company_id != link.company_id
            ) or link.event_id.company_id != link.company_id:
                raise ValidationError(
                    _("Journal entries and marketing events cannot cross companies.")
                )


class MarketingBusinessEventAccountPaymentLink(models.Model):
    _name = "marketing.business.event.account.payment.link"
    _description = "Marketing Business Event Payment Link"
    _inherit = "marketing.account.link.immutable.mixin"
    _order = "linked_at desc, id desc"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict", readonly=True
    )
    payment_id = fields.Many2one(
        "account.payment",
        index=True,
        ondelete="set null",
        check_company=True,
        readonly=True,
    )
    payment_model = fields.Char(
        required=True, size=128, default="account.payment", readonly=True
    )
    payment_res_id = fields.Integer(required=True, index=True, readonly=True)
    payment_ref = fields.Char(required=True, size=256, readonly=True)
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
            "payment_event_unique",
            "unique(company_id, payment_model, payment_res_id, event_id)",
            "This marketing event is already linked to this payment.",
        ),
        (
            "payment_res_id_positive",
            "check(payment_res_id > 0)",
            "The original payment identifier must be positive.",
        ),
    ]

    @api.constrains("company_id", "payment_id", "event_id")
    def _check_company_scope(self):
        for link in self:
            if (
                link.payment_id and link.payment_id.company_id != link.company_id
            ) or link.event_id.company_id != link.company_id:
                raise ValidationError(
                    _("Payments and marketing events cannot cross companies.")
                )


class MarketingBusinessEvent(models.Model):
    _inherit = "marketing.business.event"

    account_move_link_ids = fields.One2many(
        "marketing.business.event.account.move.link",
        "event_id",
        readonly=True,
        groups="account.group_account_invoice",
    )
    account_payment_link_ids = fields.One2many(
        "marketing.business.event.account.payment.link",
        "event_id",
        readonly=True,
        groups="account.group_account_invoice",
    )
    account_move_count = fields.Integer(
        compute="_compute_account_link_counts",
        groups="account.group_account_invoice",
    )
    account_payment_count = fields.Integer(
        compute="_compute_account_link_counts",
        groups="account.group_account_invoice",
    )

    def _compute_account_link_counts(self):
        move_grouped = self.env[
            "marketing.business.event.account.move.link"
        ].read_group(
            [("event_id", "in", self.ids), ("move_id", "!=", False)],
            ["event_id"],
            ["event_id"],
        )
        payment_grouped = self.env[
            "marketing.business.event.account.payment.link"
        ].read_group(
            [("event_id", "in", self.ids), ("payment_id", "!=", False)],
            ["event_id"],
            ["event_id"],
        )
        move_counts = {
            row["event_id"][0]: row["event_id_count"] for row in move_grouped
        }
        payment_counts = {
            row["event_id"][0]: row["event_id_count"] for row in payment_grouped
        }
        for event in self:
            event.account_move_count = move_counts.get(event.id, 0)
            event.account_payment_count = payment_counts.get(event.id, 0)

    def action_view_account_moves(self):
        self.ensure_one()
        self.check_access_rights("read")
        self.check_access_rule("read")
        return self.env["marketing.account.service"]._move_action(
            self.account_move_link_ids.mapped("move_id").exists()
        )

    def action_view_account_payments(self):
        self.ensure_one()
        self.check_access_rights("read")
        self.check_access_rule("read")
        return self.env["marketing.account.service"]._payment_action(
            self.account_payment_link_ids.mapped("payment_id").exists()
        )

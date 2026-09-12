import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.business_event_dto import BUSINESS_EVENT_CLASSES, BUSINESS_EVENT_TYPES
from ..services.tokens import MARKETING_BUSINESS_EVENT_WRITE_TOKEN
from .attribution import EVIDENCE_LEVELS


def _selection(values):
    return [(value, value.replace("_", " ").title()) for value in sorted(values)]


class NumericInteger(fields.Integer):
    """Arbitrary-size PostgreSQL integer with normal Odoo integer semantics."""

    column_type = ("numeric", "numeric(21,0)")


class ImmutableBusinessEventMixin(models.AbstractModel):
    _name = "marketing.business.event.immutable.mixin"
    _description = "Immutable Marketing Business Event Record"

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("marketing_business_event_write_token")
            is not MARKETING_BUSINESS_EVENT_WRITE_TOKEN
        ):
            raise AccessError(
                _("Marketing business events are created only by their service.")
            )
        return super().create(vals_list)

    def write(self, values):  # pylint: disable=method-required-super
        raise AccessError(_("Marketing business-event evidence cannot be edited."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Marketing business-event evidence cannot be deleted."))


class MarketingBusinessEvent(models.Model):
    _name = "marketing.business.event"
    _description = "Marketing Business Event"
    _inherit = "marketing.business.event.immutable.mixin"
    _order = "occurred_at desc, id desc"
    _rec_name = "business_event_key"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict"
    )
    public_ref = fields.Char(
        required=True,
        default=lambda self: str(uuid.uuid4()),
        size=36,
        index=True,
        readonly=True,
        copy=False,
    )
    schema_version = fields.Integer(required=True, readonly=True)
    event_class = fields.Selection(
        _selection(BUSINESS_EVENT_CLASSES), required=True, index=True, readonly=True
    )
    event_type = fields.Selection(
        _selection(BUSINESS_EVENT_TYPES), required=True, index=True, readonly=True
    )
    source_system = fields.Char(required=True, index=True, readonly=True)
    source_model = fields.Char(required=True, index=True, readonly=True)
    source_res_id = fields.Integer(required=True, index=True, readonly=True)
    source_occurrence_ref = fields.Char(required=True, index=True, readonly=True)
    source_evidence_ref = fields.Char(index=True, readonly=True)
    business_event_key = fields.Char(required=True, index=True, readonly=True)
    occurred_at = fields.Datetime(required=True, index=True, readonly=True)
    observed_at = fields.Datetime(required=True, index=True, readonly=True)
    evidence_level = fields.Selection(
        EVIDENCE_LEVELS, required=True, index=True, readonly=True
    )
    has_amount = fields.Boolean(required=True, readonly=True)
    amount_signed_micros = NumericInteger(
        required=True,
        default=0,
        readonly=True,
        group_operator=False,
        help="Canonical signed amount scaled by 1,000,000.",
    )
    amount_signed = fields.Float(
        compute="_compute_amount_signed",
        digits=(21, 6),
        group_operator=False,
        help=(
            "Display projection of the exact scaled amount. Aggregations must use "
            "amount_signed_micros to avoid binary floating-point rounding."
        ),
    )
    currency_id = fields.Many2one(
        "res.currency", index=True, ondelete="restrict", readonly=True
    )
    reverses_event_id = fields.Many2one(
        "marketing.business.event",
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    root_event_id = fields.Many2one(
        "marketing.business.event",
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    canonical_key = fields.Char(required=True, size=64, index=True, readonly=True)
    content_hash = fields.Char(required=True, size=64, index=True, readonly=True)
    snapshot_json = fields.Json(
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    observation_ids = fields.One2many(
        "marketing.business.event.observation", "event_id", readonly=True
    )

    _sql_constraints = [
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The marketing business-event reference must be unique.",
        ),
        (
            "natural_key_unique",
            "unique(company_id, source_system, business_event_key)",
            "This source business event already exists.",
        ),
        (
            "canonical_key_unique",
            "unique(company_id, canonical_key)",
            "This canonical business event already exists.",
        ),
        (
            "canonical_key_sha256",
            "check(char_length(canonical_key) = 64)",
            "The business-event canonical key must be a SHA-256 digest.",
        ),
        (
            "content_hash_sha256",
            "check(char_length(content_hash) = 64)",
            "The business-event content hash must be a SHA-256 digest.",
        ),
        (
            "positive_source_res_id",
            "check(source_res_id > 0)",
            "The business-event source record id must be positive.",
        ),
        (
            "amount_currency_pair",
            "check((has_amount AND currency_id IS NOT NULL) OR "
            "(NOT has_amount AND currency_id IS NULL AND amount_signed_micros = 0))",
            "Business-event amount and currency must be supplied together.",
        ),
    ]

    def init(self):
        self.env.cr.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "marketing_business_event_required_reversal_uniq "
            "ON marketing_business_event (reverses_event_id) "
            "WHERE event_type IN "
            "('order_cancelled', 'payment_allocation_reversed')"
        )

        # A separate index extends the contract on upgrades without replacing
        # the existing Sales/payment invariant or touching historical evidence.
        self.env.cr.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "marketing_business_event_posting_reversal_uniq "
            "ON marketing_business_event (reverses_event_id) "
            "WHERE event_type IN "
            "('invoice_posting_reversed', 'credit_note_posting_reversed')"
        )

    @api.depends("has_amount", "amount_signed_micros")
    def _compute_amount_signed(self):
        for event in self:
            event.amount_signed = (
                event.amount_signed_micros / 1_000_000 if event.has_amount else 0.0
            )

    @api.constrains(
        "company_id",
        "event_class",
        "source_model",
        "source_res_id",
        "reverses_event_id",
        "root_event_id",
    )
    def _check_reversal_chain(self):
        for event in self:
            reversed_event = event.reverses_event_id
            if not reversed_event:
                if event.root_event_id:
                    raise ValidationError(
                        _("A root event cannot reference another root.")
                    )
                continue
            expected_root = reversed_event.root_event_id or reversed_event
            if (
                reversed_event == event
                or reversed_event.company_id != event.company_id
                or reversed_event.event_class != event.event_class
                or reversed_event.source_model != event.source_model
                or (
                    event.event_type != "credit_note_posted"
                    and reversed_event.source_res_id != event.source_res_id
                )
                or event.root_event_id != expected_root
            ):
                raise ValidationError(
                    _("The business-event reversal chain is invalid.")
                )


class MarketingBusinessEventObservation(models.Model):
    _name = "marketing.business.event.observation"
    _description = "Marketing Business Event Observation"
    _inherit = "marketing.business.event.immutable.mixin"
    _order = "observed_at desc, id desc"
    _check_company_auto = True

    event_id = fields.Many2one(
        "marketing.business.event",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    company_id = fields.Many2one(
        related="event_id.company_id", store=True, index=True, readonly=True
    )
    observed_at = fields.Datetime(required=True, index=True, readonly=True)
    source_evidence_ref = fields.Char(required=True, index=True, readonly=True)
    observed_content_hash = fields.Char(required=True, size=64, readonly=True)
    disposition = fields.Selection(
        [
            ("accepted", "Accepted"),
            ("conflict", "Conflict"),
            ("duplicate", "Duplicate"),
        ],
        required=True,
        index=True,
        readonly=True,
    )
    snapshot_json = fields.Json(
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_admin",
    )

    _sql_constraints = [
        (
            "obs_unique",
            "unique(event_id, source_evidence_ref, observed_content_hash)",
            "This business-event observation already exists.",
        ),
        (
            "obs_hash_sha256",
            "check(char_length(observed_content_hash) = 64)",
            "The observed content hash must be a SHA-256 digest.",
        ),
    ]

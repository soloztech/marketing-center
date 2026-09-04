import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.attribution_calculation_dto import (
    ATTRIBUTION_EVIDENCE_BASES,
    ATTRIBUTION_STRATEGIES,
    CHANNEL_CLASSES,
)
from ..services.tokens import MARKETING_ATTRIBUTION_CALCULATION_WRITE_TOKEN
from .business_event import NumericInteger


def _selection(values):
    return [(value, value.replace("_", " ").title()) for value in sorted(values)]


ATTRIBUTION_INTERPRETATIONS = [
    ("journey_credit", "Modelled journey credit (not incrementality)"),
    ("provider_reported", "Provider-reported attribution"),
]

CANDIDATE_STATES = [
    ("credited", "Credited"),
    ("eligible_not_selected", "Eligible, not selected"),
    ("duplicate_touchpoint", "Duplicate touchpoint evidence"),
    ("excluded_after_subject", "Occurred after subject"),
    ("excluded_correlation", "Correlation only"),
    ("excluded_policy", "Excluded by model policy"),
    ("excluded_stale_revision", "Not the effective ledger revision"),
    ("excluded_window", "Outside attribution window"),
]


class ImmutableAttributionCalculationMixin(models.AbstractModel):
    _name = "marketing.attribution.calculation.immutable.mixin"
    _description = "Immutable Marketing Attribution Calculation Record"

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("marketing_attribution_calculation_write_token")
            is not MARKETING_ATTRIBUTION_CALCULATION_WRITE_TOKEN
        ):
            raise AccessError(
                _("Attribution calculations are created only by their service.")
            )
        return super().create(vals_list)

    def write(self, values):  # pylint: disable=method-required-super
        del values
        raise AccessError(_("Attribution calculations cannot be edited."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Attribution calculations cannot be deleted."))


class MarketingAttributionModel(models.Model):
    _name = "marketing.attribution.model"
    _description = "Versioned Marketing Attribution Model"
    _inherit = "marketing.attribution.calculation.immutable.mixin"
    _order = "code, version desc, id desc"
    _rec_name = "name"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict", readonly=True
    )
    name = fields.Char(required=True, readonly=True)
    code = fields.Char(required=True, size=64, index=True, readonly=True)
    version = fields.Integer(required=True, index=True, readonly=True)
    schema_version = fields.Integer(required=True, readonly=True)
    strategy = fields.Selection(
        _selection(ATTRIBUTION_STRATEGIES), required=True, index=True, readonly=True
    )
    interpretation = fields.Selection(
        ATTRIBUTION_INTERPRETATIONS, required=True, index=True, readonly=True
    )
    window_days = fields.Integer(required=True, readonly=True)
    policy_ref = fields.Char(required=True, size=128, index=True, readonly=True)
    registered_by_producer = fields.Char(
        required=True, size=128, index=True, readonly=True
    )
    eligible_evidence_bases_json = fields.Json(required=True, readonly=True)
    content_hash = fields.Char(required=True, size=64, index=True, readonly=True)
    created_at = fields.Datetime(required=True, index=True, readonly=True)

    _sql_constraints = [
        (
            "code_version_unique",
            "unique(company_id, code, version)",
            "This attribution model version already exists for the company.",
        ),
        (
            "content_hash_sha256",
            "check(char_length(content_hash) = 64)",
            "The attribution-model content hash must be a SHA-256 digest.",
        ),
        (
            "version_positive",
            "check(version > 0 AND schema_version > 0)",
            "Attribution model versions must be positive.",
        ),
        (
            "window_positive",
            "check(window_days > 0 AND window_days <= 3650)",
            "The attribution window must be between one and 3650 days.",
        ),
    ]

    @api.constrains("strategy", "interpretation", "eligible_evidence_bases_json")
    def _check_interpretation_boundary(self):
        for model in self:
            expected = (
                "provider_reported"
                if model.strategy == "platform_reported"
                else "journey_credit"
            )
            bases = set(model.eligible_evidence_bases_json or ())
            if model.interpretation != expected:
                raise ValidationError(
                    _("Provider-reported and journey-credit models cannot be mixed.")
                )
            if model.strategy == "platform_reported":
                valid_bases = bases == {"provider_reported"}
            else:
                valid_bases = bool(bases) and bases <= {"deterministic_first_party"}
            if not valid_bases:
                raise ValidationError(
                    _("The model has evidence bases outside its interpretation scope.")
                )


class MarketingAttributionCalculationRun(models.Model):
    _name = "marketing.attribution.calculation.run"
    _description = "Immutable Marketing Attribution Calculation Run"
    _inherit = "marketing.attribution.calculation.immutable.mixin"
    _order = "calculated_at desc, id desc"
    _rec_name = "public_ref"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict", readonly=True
    )
    public_ref = fields.Char(
        required=True,
        default=lambda self: str(uuid.uuid4()),
        size=36,
        index=True,
        readonly=True,
        copy=False,
    )
    calculation_ref = fields.Char(required=True, size=512, index=True, readonly=True)
    schema_version = fields.Integer(required=True, readonly=True)
    model_id = fields.Many2one(
        "marketing.attribution.model",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    model_code = fields.Char(required=True, size=64, index=True, readonly=True)
    model_version = fields.Integer(required=True, index=True, readonly=True)
    policy_ref = fields.Char(required=True, size=128, index=True, readonly=True)
    producer_key = fields.Char(required=True, size=128, index=True, readonly=True)
    interpretation = fields.Selection(
        ATTRIBUTION_INTERPRETATIONS, required=True, index=True, readonly=True
    )
    observed_at = fields.Datetime(required=True, index=True, readonly=True)
    calculated_at = fields.Datetime(required=True, index=True, readonly=True)
    input_hash = fields.Char(required=True, size=64, index=True, readonly=True)
    result_ids = fields.One2many(
        "marketing.attribution.result", "run_id", readonly=True
    )

    _sql_constraints = [
        (
            "calculation_ref_unique",
            "unique(company_id, calculation_ref)",
            "This attribution calculation reference already exists.",
        ),
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The attribution calculation public reference must be unique.",
        ),
        (
            "input_hash_sha256",
            "check(char_length(input_hash) = 64)",
            "The attribution calculation input hash must be a SHA-256 digest.",
        ),
        (
            "version_positive",
            "check(schema_version > 0 AND model_version > 0)",
            "Attribution calculation versions must be positive.",
        ),
    ]

    @api.constrains(
        "company_id",
        "model_id",
        "model_code",
        "model_version",
        "policy_ref",
        "interpretation",
    )
    def _check_model_snapshot(self):
        for run in self:
            if (
                run.model_id.company_id != run.company_id
                or run.model_id.code != run.model_code
                or run.model_id.version != run.model_version
                or run.model_id.policy_ref != run.policy_ref
                or run.model_id.interpretation != run.interpretation
            ):
                raise ValidationError(
                    _("The calculation run and model snapshot are inconsistent.")
                )


class MarketingAttributionResult(models.Model):
    _name = "marketing.attribution.result"
    _description = "Immutable Marketing Attribution Result"
    _inherit = "marketing.attribution.calculation.immutable.mixin"
    _order = "calculated_at desc, id desc"
    _rec_name = "subject_key"
    _check_company_auto = True

    run_id = fields.Many2one(
        "marketing.attribution.calculation.run",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    company_id = fields.Many2one(
        related="run_id.company_id", store=True, index=True, readonly=True
    )
    model_id = fields.Many2one(
        related="run_id.model_id", store=True, index=True, readonly=True
    )
    business_event_id = fields.Many2one(
        "marketing.business.event",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    subject_kind = fields.Selection(
        [("business_event", "Business event")],
        required=True,
        default="business_event",
        readonly=True,
    )
    subject_key = fields.Char(required=True, size=512, index=True, readonly=True)
    window_start = fields.Datetime(required=True, index=True, readonly=True)
    window_end = fields.Datetime(required=True, index=True, readonly=True)
    calculated_at = fields.Datetime(required=True, index=True, readonly=True)
    disposition = fields.Selection(
        [("credited", "Credited"), ("unattributed", "Unattributed")],
        required=True,
        index=True,
        readonly=True,
    )
    unattributed_reason = fields.Selection(
        [
            ("no_candidates", "No candidate evidence"),
            ("no_eligible_candidates", "No eligible candidate evidence"),
            ("none", "Not applicable"),
        ],
        required=True,
        readonly=True,
    )
    candidate_count = fields.Integer(required=True, readonly=True)
    eligible_count = fields.Integer(required=True, readonly=True)
    credited_count = fields.Integer(required=True, readonly=True)
    total_weight_micros = fields.Integer(required=True, readonly=True)
    has_amount = fields.Boolean(required=True, readonly=True)
    subject_amount_micros = NumericInteger(required=True, readonly=True)
    attributed_amount_micros = NumericInteger(required=True, readonly=True)
    currency_id = fields.Many2one(
        "res.currency", index=True, ondelete="restrict", readonly=True
    )
    candidate_ids = fields.One2many(
        "marketing.attribution.candidate", "result_id", readonly=True
    )
    contribution_ids = fields.One2many(
        "marketing.attribution.contribution", "result_id", readonly=True
    )

    _sql_constraints = [
        (
            "one_result_per_run",
            "unique(run_id)",
            "This calculation run already has a result.",
        ),
        (
            "counts_nonnegative",
            "check(candidate_count >= 0 AND eligible_count >= 0 "
            "AND credited_count >= 0 AND eligible_count <= candidate_count "
            "AND credited_count <= eligible_count)",
            "Attribution result counts are inconsistent.",
        ),
        (
            "weight_valid",
            "check(total_weight_micros IN (0, 1000000))",
            "Attribution weight must be zero or exactly one million micros.",
        ),
        (
            "amount_currency_pair",
            "check((has_amount AND currency_id IS NOT NULL) OR "
            "(NOT has_amount AND currency_id IS NULL AND subject_amount_micros = 0 "
            "AND attributed_amount_micros = 0))",
            "Attribution amount and currency must be supplied together.",
        ),
        (
            "credited_shape",
            "check((disposition = 'credited' AND unattributed_reason = 'none' "
            "AND credited_count > 0 AND total_weight_micros = 1000000 "
            "AND attributed_amount_micros = subject_amount_micros) OR "
            "(disposition = 'unattributed' AND unattributed_reason <> 'none' "
            "AND credited_count = 0 AND total_weight_micros = 0 "
            "AND attributed_amount_micros = 0))",
            "The attributed and unattributed result shapes are inconsistent.",
        ),
    ]

    @api.constrains(
        "company_id", "run_id", "business_event_id", "window_start", "window_end"
    )
    def _check_subject_scope(self):
        for result in self:
            if (
                result.run_id.company_id != result.company_id
                or result.business_event_id.company_id != result.company_id
            ):
                raise ValidationError(
                    _("The attribution result and subject must share a company.")
                )
            if (
                result.window_start > result.window_end
                or result.window_end > result.business_event_id.occurred_at
            ):
                raise ValidationError(
                    _("The attribution window cannot extend beyond the subject event.")
                )


class MarketingAttributionCandidate(models.Model):
    _name = "marketing.attribution.candidate"
    _description = "Immutable Marketing Attribution Candidate"
    _inherit = "marketing.attribution.calculation.immutable.mixin"
    _order = "candidate_sequence, id"
    _check_company_auto = True

    result_id = fields.Many2one(
        "marketing.attribution.result",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    company_id = fields.Many2one(
        related="result_id.company_id", store=True, index=True, readonly=True
    )
    touchpoint_id = fields.Many2one(
        "marketing.attribution.touchpoint",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    candidate_key = fields.Char(required=True, size=64, index=True, readonly=True)
    candidate_sequence = fields.Integer(required=True, readonly=True)
    evidence_basis = fields.Selection(
        _selection(ATTRIBUTION_EVIDENCE_BASES),
        required=True,
        index=True,
        readonly=True,
    )
    evidence_ref = fields.Char(required=True, size=512, index=True, readonly=True)
    channel_class = fields.Selection(
        _selection(CHANNEL_CLASSES), required=True, index=True, readonly=True
    )
    reported_weight_micros = fields.Integer(readonly=True)
    touchpoint_occurred_at = fields.Datetime(required=True, index=True, readonly=True)
    state = fields.Selection(CANDIDATE_STATES, required=True, index=True, readonly=True)
    reason = fields.Char(required=True, size=128, index=True, readonly=True)

    _sql_constraints = [
        (
            "candidate_key_unique",
            "unique(result_id, candidate_key)",
            "This candidate evidence already exists in the result.",
        ),
        (
            "candidate_key_sha256",
            "check(char_length(candidate_key) = 64)",
            "The attribution candidate key must be a SHA-256 digest.",
        ),
        (
            "sequence_positive",
            "check(candidate_sequence > 0)",
            "The attribution candidate sequence must be positive.",
        ),
        (
            "reported_weight_range",
            "check(reported_weight_micros IS NULL OR "
            "(reported_weight_micros >= 0 AND reported_weight_micros <= 1000000))",
            "Provider-reported weight must be between zero and one million micros.",
        ),
    ]

    @api.constrains("company_id", "result_id", "touchpoint_id")
    def _check_touchpoint_scope(self):
        for candidate in self:
            if (
                candidate.result_id.company_id != candidate.company_id
                or candidate.touchpoint_id.company_id != candidate.company_id
            ):
                raise ValidationError(
                    _("The candidate, result and touchpoint must share a company.")
                )


class MarketingAttributionContribution(models.Model):
    _name = "marketing.attribution.contribution"
    _description = "Immutable Marketing Attribution Contribution"
    _inherit = "marketing.attribution.calculation.immutable.mixin"
    _order = "contribution_sequence, id"
    _check_company_auto = True

    result_id = fields.Many2one(
        "marketing.attribution.result",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    company_id = fields.Many2one(
        related="result_id.company_id", store=True, index=True, readonly=True
    )
    candidate_id = fields.Many2one(
        "marketing.attribution.candidate",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    touchpoint_id = fields.Many2one(
        "marketing.attribution.touchpoint",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    contribution_sequence = fields.Integer(required=True, readonly=True)
    weight_micros = fields.Integer(required=True, readonly=True)
    allocated_amount_micros = NumericInteger(required=True, readonly=True)
    evidence_basis = fields.Selection(
        _selection(ATTRIBUTION_EVIDENCE_BASES), required=True, readonly=True
    )
    platform = fields.Char(required=True, index=True, readonly=True)
    channel = fields.Char(required=True, index=True, readonly=True)
    touchpoint_type = fields.Char(required=True, index=True, readonly=True)
    touchpoint_occurred_at = fields.Datetime(required=True, index=True, readonly=True)

    _sql_constraints = [
        (
            "touchpoint_once_per_result",
            "unique(result_id, touchpoint_id)",
            "A touchpoint can receive credit only once per result.",
        ),
        (
            "sequence_positive",
            "check(contribution_sequence > 0)",
            "The contribution sequence must be positive.",
        ),
        (
            "weight_positive",
            "check(weight_micros > 0 AND weight_micros <= 1000000)",
            "Contribution weight must be positive and no greater than one million.",
        ),
    ]

    @api.constrains("company_id", "result_id", "candidate_id", "touchpoint_id")
    def _check_contribution_scope(self):
        for contribution in self:
            if (
                contribution.result_id.company_id != contribution.company_id
                or contribution.candidate_id.result_id != contribution.result_id
                or contribution.candidate_id.touchpoint_id != contribution.touchpoint_id
                or contribution.touchpoint_id.company_id != contribution.company_id
            ):
                raise ValidationError(
                    _("The attribution contribution references inconsistent evidence.")
                )

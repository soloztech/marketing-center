import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from ..services.tokens import MARKETING_ATTRIBUTION_WRITE_TOKEN

TOUCHPOINT_TYPES = [
    ("conversation_start", "Conversation start"),
    ("entry_point", "Entry point"),
    ("form_submission", "Form submission"),
    ("lead_ad", "Lead ad"),
    ("organic_link", "Organic link"),
    ("paid_ad_click", "Paid ad click"),
    ("paid_ad_signal", "Paid ad signal"),
    ("unknown", "Unknown"),
]
EVIDENCE_LEVELS = [
    ("derived", "Derived"),
    ("first_party", "First party"),
    ("imported", "Imported"),
    ("observed", "Observed"),
    ("provider_asserted", "Provider asserted"),
    ("provider_asserted_non_paid", "Provider asserted non-paid"),
    ("provider_hint", "Provider hint"),
]


class ImmutableAttributionMixin(models.AbstractModel):
    _name = "marketing.attribution.immutable.mixin"
    _description = "Immutable Marketing Attribution Record"

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("marketing_attribution_write_token")
            is not MARKETING_ATTRIBUTION_WRITE_TOKEN
        ):
            raise AccessError(
                _("Marketing attribution evidence is created only by its service.")
            )
        return super().create(vals_list)

    def write(self, values):  # pylint: disable=method-required-super
        raise AccessError(_("Marketing attribution evidence cannot be edited."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Marketing attribution evidence cannot be deleted."))


class MarketingAttributionTouchpoint(models.Model):
    _name = "marketing.attribution.touchpoint"
    _description = "Marketing Attribution Touchpoint"
    _inherit = "marketing.attribution.immutable.mixin"
    _order = "occurred_at desc, revision_sequence desc, id desc"
    _rec_name = "public_ref"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict"
    )
    public_ref = fields.Char(
        required=True,
        default=lambda self: str(uuid.uuid4()),
        size=36,
        index=True,
        copy=False,
        readonly=True,
    )
    schema_version = fields.Integer(required=True, readonly=True)
    mapping_version = fields.Integer(required=True, readonly=True)
    source_system = fields.Char(required=True, index=True, readonly=True)
    source_scope_ref = fields.Char(required=True, index=True, readonly=True)
    source_occurrence_ref = fields.Char(required=True, index=True, readonly=True)
    source_evidence_ref = fields.Char(index=True, readonly=True)
    source_schema_version = fields.Char(readonly=True)
    occurred_at = fields.Datetime(required=True, index=True, readonly=True)
    observed_at = fields.Datetime(required=True, index=True, readonly=True)
    platform = fields.Char(required=True, index=True, readonly=True)
    channel = fields.Char(required=True, index=True, readonly=True)
    network = fields.Char(index=True, readonly=True)
    touchpoint_type = fields.Selection(
        TOUCHPOINT_TYPES, required=True, index=True, readonly=True
    )
    evidence_level = fields.Selection(
        EVIDENCE_LEVELS, required=True, index=True, readonly=True
    )
    landing_url = fields.Char(readonly=True)
    referrer_url = fields.Char(readonly=True)
    utm_source = fields.Char(index=True, readonly=True)
    utm_medium = fields.Char(index=True, readonly=True)
    utm_campaign = fields.Char(index=True, readonly=True)
    utm_content = fields.Char(readonly=True)
    utm_term = fields.Char(readonly=True)
    asset_refs_json = fields.Json(readonly=True, copy=False)
    policy_version = fields.Char(readonly=True)
    notice_version = fields.Char(readonly=True)
    legal_basis_code = fields.Char(index=True, readonly=True)
    consent_state = fields.Selection(
        [("denied", "Denied"), ("granted", "Granted"), ("unknown", "Unknown")],
        required=True,
        default="unknown",
        index=True,
        readonly=True,
    )
    privacy_decision_source = fields.Char(readonly=True)
    privacy_decided_at = fields.Datetime(readonly=True)
    extensions_json = fields.Json(
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    canonical_key = fields.Char(required=True, size=64, index=True, readonly=True)
    content_hash = fields.Char(required=True, size=64, index=True, readonly=True)
    revision_sequence = fields.Integer(required=True, index=True, readonly=True)
    identifier_ids = fields.One2many(
        "marketing.attribution.identifier",
        "touchpoint_id",
        readonly=True,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    evidence_ids = fields.One2many(
        "marketing.attribution.evidence", "touchpoint_id", readonly=True
    )

    _sql_constraints = [
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The marketing touchpoint public reference must be unique.",
        ),
        (
            "canonical_content_unique",
            "unique(company_id, canonical_key, content_hash)",
            "This marketing touchpoint revision already exists.",
        ),
        (
            "canonical_revision_unique",
            "unique(company_id, canonical_key, revision_sequence)",
            "This marketing touchpoint revision sequence already exists.",
        ),
        (
            "canonical_key_sha256",
            "check(char_length(canonical_key) = 64)",
            "The canonical key must be a SHA-256 digest.",
        ),
        (
            "content_hash_sha256",
            "check(char_length(content_hash) = 64)",
            "The content hash must be a SHA-256 digest.",
        ),
        (
            "revision_sequence_positive",
            "check(revision_sequence > 0)",
            "The revision sequence must be positive.",
        ),
    ]


class MarketingAttributionIdentifier(models.Model):
    _name = "marketing.attribution.identifier"
    _description = "Marketing Attribution Identifier"
    _inherit = "marketing.attribution.immutable.mixin"
    _order = "touchpoint_id, namespace, role, id"
    _check_company_auto = True

    touchpoint_id = fields.Many2one(
        "marketing.attribution.touchpoint",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    company_id = fields.Many2one(
        related="touchpoint_id.company_id", store=True, readonly=True, index=True
    )
    namespace = fields.Char(required=True, index=True, readonly=True)
    role = fields.Char(required=True, index=True, readonly=True)
    comparison_hash = fields.Char(required=True, size=64, index=True, readonly=True)
    masked_value = fields.Char(readonly=True)
    value_ref = fields.Char(readonly=True)
    source_field = fields.Char(readonly=True)
    purpose = fields.Char(required=True, index=True, readonly=True)
    observed_at = fields.Datetime(required=True, index=True, readonly=True)
    retain_until = fields.Date(index=True, readonly=True)

    _sql_constraints = [
        (
            "touchpoint_identifier_unique",
            "unique(touchpoint_id, namespace, role, comparison_hash)",
            "This marketing identifier already exists on the touchpoint.",
        ),
        (
            "comparison_hash_sha256",
            "check(char_length(comparison_hash) = 64)",
            "The comparison hash must be a SHA-256 digest.",
        ),
    ]


class MarketingAttributionEvidence(models.Model):
    _name = "marketing.attribution.evidence"
    _description = "Marketing Attribution Evidence"
    _inherit = "marketing.attribution.immutable.mixin"
    _order = "observed_at desc, id desc"
    _check_company_auto = True

    touchpoint_id = fields.Many2one(
        "marketing.attribution.touchpoint",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    company_id = fields.Many2one(
        related="touchpoint_id.company_id", store=True, readonly=True, index=True
    )
    source_system = fields.Char(required=True, index=True, readonly=True)
    source_evidence_ref = fields.Char(required=True, index=True, readonly=True)
    evidence_digest = fields.Char(required=True, size=64, index=True, readonly=True)
    mapping_version = fields.Integer(required=True, readonly=True)
    observed_at = fields.Datetime(required=True, index=True, readonly=True)
    disposition = fields.Selection(
        [
            ("accepted", "Accepted"),
            ("conflict", "Conflict"),
            ("duplicate", "Duplicate observation"),
        ],
        required=True,
        index=True,
        readonly=True,
    )
    related_touchpoint_id = fields.Many2one(
        "marketing.attribution.touchpoint",
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )

    _sql_constraints = [
        (
            "touchpoint_evidence_unique",
            "unique(touchpoint_id, source_evidence_ref, evidence_digest)",
            "This marketing evidence was already recorded.",
        ),
        (
            "evidence_digest_sha256",
            "check(char_length(evidence_digest) = 64)",
            "The evidence digest must be a SHA-256 digest.",
        ),
        (
            "mapping_version_positive",
            "check(mapping_version > 0)",
            "The evidence mapping version must be positive.",
        ),
    ]

from odoo import _, api, fields, models, tools
from odoo.exceptions import AccessError

from .attribution import EVIDENCE_LEVELS, REVISION_KINDS, TOUCHPOINT_TYPES

EFFECTIVE_DISPOSITIONS = [
    ("accepted", "Accepted"),
    ("enriched", "Enriched"),
    ("revised", "Revised"),
]


class MarketingAttributionEffectiveTouchpoint(models.Model):
    """Current accepted projection over the immutable attribution ledger.

    The record ``id`` deliberately equals the winning physical touchpoint id.
    Consumers can therefore use the projection as a safe current-state index
    without changing the immutable revision/evidence tables.
    """

    _name = "marketing.attribution.effective.touchpoint"
    _description = "Effective Marketing Attribution Touchpoint"
    _auto = False
    _table = "marketing_attribution_effective_touchpoint"
    _order = "occurred_at desc, revision_sequence desc, id desc"
    _rec_name = "public_ref"
    _check_company_auto = True

    touchpoint_id = fields.Many2one(
        "marketing.attribution.touchpoint",
        string="Ledger revision",
        required=True,
        readonly=True,
        ondelete="restrict",
        check_company=True,
    )
    company_id = fields.Many2one("res.company", required=True, readonly=True)
    public_ref = fields.Char(readonly=True)
    schema_version = fields.Integer(readonly=True)
    mapping_version = fields.Integer(readonly=True)
    source_system = fields.Char(readonly=True)
    source_scope_ref = fields.Char(readonly=True)
    source_occurrence_ref = fields.Char(readonly=True)
    source_evidence_ref = fields.Char(readonly=True)
    source_schema_version = fields.Char(readonly=True)
    occurred_at = fields.Datetime(readonly=True)
    observed_at = fields.Datetime(readonly=True)
    platform = fields.Char(readonly=True)
    channel = fields.Char(readonly=True)
    network = fields.Char(readonly=True)
    touchpoint_type = fields.Selection(TOUCHPOINT_TYPES, readonly=True)
    evidence_level = fields.Selection(EVIDENCE_LEVELS, readonly=True)
    revision_kind = fields.Selection(REVISION_KINDS, readonly=True)
    effective_disposition = fields.Selection(EFFECTIVE_DISPOSITIONS, readonly=True)
    landing_url = fields.Char(readonly=True)
    referrer_url = fields.Char(readonly=True)
    utm_source = fields.Char(readonly=True)
    utm_medium = fields.Char(readonly=True)
    utm_campaign = fields.Char(readonly=True)
    utm_content = fields.Char(readonly=True)
    utm_term = fields.Char(readonly=True)
    asset_refs_json = fields.Json(readonly=True)
    policy_version = fields.Char(readonly=True)
    notice_version = fields.Char(readonly=True)
    legal_basis_code = fields.Char(readonly=True)
    consent_state = fields.Selection(
        [("denied", "Denied"), ("granted", "Granted"), ("unknown", "Unknown")],
        readonly=True,
    )
    privacy_decision_source = fields.Char(readonly=True)
    privacy_decided_at = fields.Datetime(readonly=True)
    extensions_json = fields.Json(
        readonly=True,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    canonical_key = fields.Char(readonly=True)
    content_hash = fields.Char(readonly=True)
    revision_sequence = fields.Integer(readonly=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(
            """
            CREATE VIEW marketing_attribution_effective_touchpoint AS
            WITH eligible AS (
                SELECT
                    touchpoint.*,
                    evidence.disposition AS effective_disposition,
                    ROW_NUMBER() OVER (
                        PARTITION BY touchpoint.company_id, touchpoint.canonical_key
                        ORDER BY
                            touchpoint.revision_sequence DESC,
                            touchpoint.id DESC
                    ) AS effective_rank
                FROM marketing_attribution_touchpoint AS touchpoint
                JOIN LATERAL (
                    SELECT item.disposition
                    FROM marketing_attribution_evidence AS item
                    WHERE item.touchpoint_id = touchpoint.id
                      AND item.disposition IN ('accepted', 'enriched', 'revised')
                    ORDER BY item.observed_at DESC, item.id DESC
                    LIMIT 1
                ) AS evidence ON TRUE
            )
            SELECT
                eligible.id,
                eligible.id AS touchpoint_id,
                eligible.company_id,
                eligible.public_ref,
                eligible.schema_version,
                eligible.mapping_version,
                eligible.source_system,
                eligible.source_scope_ref,
                eligible.source_occurrence_ref,
                eligible.source_evidence_ref,
                eligible.source_schema_version,
                eligible.occurred_at,
                eligible.observed_at,
                eligible.platform,
                eligible.channel,
                eligible.network,
                eligible.touchpoint_type,
                eligible.evidence_level,
                eligible.revision_kind,
                eligible.effective_disposition,
                eligible.landing_url,
                eligible.referrer_url,
                eligible.utm_source,
                eligible.utm_medium,
                eligible.utm_campaign,
                eligible.utm_content,
                eligible.utm_term,
                eligible.asset_refs_json,
                eligible.policy_version,
                eligible.notice_version,
                eligible.legal_basis_code,
                eligible.consent_state,
                eligible.privacy_decision_source,
                eligible.privacy_decided_at,
                eligible.extensions_json,
                eligible.canonical_key,
                eligible.content_hash,
                eligible.revision_sequence
            FROM eligible
            WHERE eligible.effective_rank = 1
            """
        )
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS marketing_attr_evidence_effective_idx
            ON marketing_attribution_evidence (touchpoint_id, observed_at DESC, id DESC)
            WHERE disposition IN ('accepted', 'enriched', 'revised')
            """
        )

    @api.model_create_multi
    def create(self, vals_list):  # pylint: disable=method-required-super
        del vals_list
        raise AccessError(_("The effective touchpoint projection is read-only."))

    def write(self, values):  # pylint: disable=method-required-super
        del values
        raise AccessError(_("The effective touchpoint projection is read-only."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("The effective touchpoint projection is read-only."))

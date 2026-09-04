from odoo import _, api, fields, models, tools
from odoo.exceptions import AccessError, ValidationError

from .tokens import MARKETING_CRM_LINK_WRITE_TOKEN


class ImmutableMarketingCrmLinkMixin(models.AbstractModel):
    _name = "marketing.crm.link.immutable.mixin"
    _description = "Immutable Marketing CRM Link"

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("marketing_crm_link_write_token")
            is not MARKETING_CRM_LINK_WRITE_TOKEN
        ):
            raise AccessError(
                _("Marketing CRM links are created only by their service.")
            )
        return super().create(vals_list)

    def write(self, values):  # pylint: disable=method-required-super
        raise AccessError(_("Marketing CRM links cannot be edited."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Marketing CRM links cannot be deleted."))


class MarketingAttributionCrmLink(models.Model):
    _name = "marketing.attribution.crm.link"
    _description = "Marketing Attribution CRM Link Assertion"
    _inherit = "marketing.crm.link.immutable.mixin"
    _order = "linked_at desc, id desc"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict", readonly=True
    )
    touchpoint_id = fields.Many2one(
        "marketing.attribution.touchpoint",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    lead_id = fields.Many2one(
        "crm.lead",
        index=True,
        ondelete="set null",
        check_company=True,
        readonly=True,
    )
    lead_model = fields.Char(required=True, size=64, readonly=True)
    lead_res_id = fields.Integer(required=True, index=True, readonly=True)
    lead_display_ref = fields.Char(required=True, size=256, readonly=True)
    canonical_key = fields.Char(
        required=True,
        size=64,
        index=True,
        readonly=True,
        help="Immutable canonical touchpoint identity asserted for the CRM lead.",
    )
    authority_key = fields.Char(
        required=True,
        size=128,
        index=True,
        readonly=True,
        help="Bounded authority that owns this assertion and may revoke it.",
    )
    authority_ref = fields.Char(
        required=True,
        size=512,
        index=True,
        readonly=True,
        help="Stable record reference inside the assertion authority.",
    )
    assertion_ref = fields.Char(
        required=True,
        size=256,
        index=True,
        readonly=True,
        help="Idempotency reference unique inside the assertion authority.",
    )
    source_ref = fields.Char(required=True, size=512, index=True, readonly=True)
    linked_at = fields.Datetime(
        required=True, default=fields.Datetime.now, index=True, readonly=True
    )
    revocation_ids = fields.One2many(
        "marketing.attribution.crm.revocation", "assertion_id", readonly=True
    )
    derived_from_assertion_id = fields.Many2one(
        "marketing.attribution.crm.link",
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
        help="Original authority assertion re-projected by an explicit CRM merge.",
    )

    def init(self):
        """Create indexes for the canonical immutable assertion model."""

        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                marketing_attribution_crm_assertion_identity_uniq
            ON marketing_attribution_crm_link
                (company_id, authority_key, assertion_ref)
            """
        )
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS
                marketing_attribution_crm_assertion_projection_idx
            ON marketing_attribution_crm_link
                (company_id, canonical_key, lead_id)
            """
        )

    @api.constrains(
        "company_id",
        "touchpoint_id",
        "lead_id",
        "lead_model",
        "lead_res_id",
        "lead_display_ref",
        "canonical_key",
        "authority_key",
        "authority_ref",
        "assertion_ref",
        "derived_from_assertion_id",
    )
    def _check_company_scope(self):
        for link in self:
            if (
                link.lead_model != "crm.lead"
                or link.lead_res_id <= 0
                or not (link.lead_display_ref or "").strip()
                or (link.lead_id and link.lead_id.id != link.lead_res_id)
                or link.touchpoint_id.company_id != link.company_id
                or (
                    link.lead_id.company_id
                    and link.lead_id.company_id != link.company_id
                )
            ):
                raise ValidationError(
                    _("Marketing touchpoints and CRM leads cannot cross companies.")
                )
            if link.canonical_key != link.touchpoint_id.canonical_key:
                raise ValidationError(
                    _("The CRM assertion canonical touchpoint identity is invalid.")
                )
            if not all(
                (value or "").strip()
                for value in (
                    link.authority_key,
                    link.authority_ref,
                    link.assertion_ref,
                )
            ):
                raise ValidationError(
                    _("The CRM assertion authority identity is incomplete.")
                )
            source = link.derived_from_assertion_id
            if source and (
                source == link
                or source.derived_from_assertion_id
                or source.company_id != link.company_id
                or source.canonical_key != link.canonical_key
            ):
                raise ValidationError(_("The CRM merge assertion lineage is invalid."))


class MarketingAttributionCrmRevocation(models.Model):
    _name = "marketing.attribution.crm.revocation"
    _description = "Marketing Attribution CRM Link Revocation"
    _inherit = "marketing.crm.link.immutable.mixin"
    _order = "revoked_at desc, id desc"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict", readonly=True
    )
    assertion_id = fields.Many2one(
        "marketing.attribution.crm.link",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    lead_id = fields.Many2one(
        "crm.lead",
        index=True,
        ondelete="set null",
        check_company=True,
        readonly=True,
    )
    lead_model = fields.Char(required=True, size=64, readonly=True)
    lead_res_id = fields.Integer(required=True, index=True, readonly=True)
    lead_display_ref = fields.Char(required=True, size=256, readonly=True)
    authority_key = fields.Char(required=True, size=128, index=True, readonly=True)
    authority_ref = fields.Char(required=True, size=512, index=True, readonly=True)
    revocation_ref = fields.Char(required=True, size=256, index=True, readonly=True)
    reason = fields.Char(required=True, size=512, readonly=True)
    revoked_at = fields.Datetime(
        required=True, default=fields.Datetime.now, index=True, readonly=True
    )

    _sql_constraints = [
        (
            "assertion_once_unique",
            "unique(assertion_id)",
            "This CRM assertion has already been revoked.",
        ),
        (
            "authority_ref_unique",
            "unique(company_id, authority_key, revocation_ref)",
            "This CRM assertion revocation has already been recorded.",
        ),
    ]

    @api.constrains(
        "company_id",
        "assertion_id",
        "lead_id",
        "lead_model",
        "lead_res_id",
        "lead_display_ref",
        "authority_key",
        "authority_ref",
    )
    def _check_revocation_scope(self):
        for revocation in self:
            assertion = revocation.assertion_id
            if (
                assertion.company_id != revocation.company_id
                or revocation.lead_model != "crm.lead"
                or revocation.lead_res_id <= 0
                or not (revocation.lead_display_ref or "").strip()
                or (
                    revocation.lead_id
                    and revocation.lead_id.id != revocation.lead_res_id
                )
                or assertion.lead_model != revocation.lead_model
                or assertion.lead_res_id != revocation.lead_res_id
                or assertion.lead_display_ref != revocation.lead_display_ref
                or assertion.authority_key != revocation.authority_key
                or assertion.authority_ref != revocation.authority_ref
            ):
                raise ValidationError(
                    _("Only an assertion's own authority may revoke it.")
                )


class MarketingAttributionCrmEffectiveLink(models.Model):
    """Current link projection over append-only assertions and revocations."""

    _name = "marketing.attribution.crm.effective.link"
    _description = "Effective Marketing Attribution CRM Link"
    _auto = False
    _table = "marketing_attribution_crm_effective_link"
    _order = "linked_at desc, id desc"
    _check_company_auto = True

    company_id = fields.Many2one("res.company", required=True, readonly=True)
    canonical_key = fields.Char(readonly=True)
    touchpoint_id = fields.Many2one(
        "marketing.attribution.effective.touchpoint",
        required=True,
        readonly=True,
        ondelete="restrict",
        check_company=True,
    )
    lead_id = fields.Many2one(
        "crm.lead",
        required=True,
        readonly=True,
        ondelete="restrict",
        check_company=True,
    )
    assertion_count = fields.Integer(readonly=True)
    linked_at = fields.Datetime(readonly=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(
            """
            CREATE VIEW marketing_attribution_crm_effective_link AS
            WITH eligible_touchpoint AS (
                SELECT
                    touchpoint.id,
                    touchpoint.company_id,
                    touchpoint.canonical_key,
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            touchpoint.company_id,
                            touchpoint.canonical_key
                        ORDER BY
                            touchpoint.revision_sequence DESC,
                            touchpoint.id DESC
                    ) AS effective_rank
                FROM marketing_attribution_touchpoint AS touchpoint
                JOIN LATERAL (
                    SELECT evidence.disposition
                    FROM marketing_attribution_evidence AS evidence
                    WHERE evidence.touchpoint_id = touchpoint.id
                      AND evidence.disposition IN
                          ('accepted', 'enriched', 'revised')
                    ORDER BY evidence.observed_at DESC, evidence.id DESC
                    LIMIT 1
                ) AS accepted_evidence ON TRUE
            ),
            effective_touchpoint AS (
                SELECT id, company_id, canonical_key
                FROM eligible_touchpoint
                WHERE effective_rank = 1
            )
            SELECT
                min(assertion.id) AS id,
                assertion.company_id,
                assertion.canonical_key,
                effective.id AS touchpoint_id,
                assertion.lead_id,
                count(assertion.id)::integer AS assertion_count,
                min(assertion.linked_at) AS linked_at
            FROM marketing_attribution_crm_link AS assertion
            JOIN effective_touchpoint AS effective
              ON effective.company_id = assertion.company_id
             AND effective.canonical_key = assertion.canonical_key
            LEFT JOIN marketing_attribution_crm_revocation AS revocation
              ON revocation.assertion_id = assertion.id
            LEFT JOIN marketing_attribution_crm_revocation AS source_revocation
              ON source_revocation.assertion_id = assertion.derived_from_assertion_id
            WHERE revocation.id IS NULL
              AND source_revocation.id IS NULL
              AND assertion.lead_id IS NOT NULL
            GROUP BY
                assertion.company_id,
                assertion.canonical_key,
                effective.id,
                assertion.lead_id
            """
        )

    @api.model_create_multi
    def create(self, vals_list):  # pylint: disable=method-required-super
        del vals_list
        raise AccessError(_("The effective CRM attribution projection is read-only."))

    def write(self, values):  # pylint: disable=method-required-super
        del values
        raise AccessError(_("The effective CRM attribution projection is read-only."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("The effective CRM attribution projection is read-only."))


class MarketingBusinessEventCrmLink(models.Model):
    _name = "marketing.business.event.crm.link"
    _description = "Marketing Business Event CRM Link"
    _inherit = "marketing.crm.link.immutable.mixin"
    _order = "linked_at desc, id desc"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict", readonly=True
    )
    event_id = fields.Many2one(
        "marketing.business.event",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    lead_id = fields.Many2one(
        "crm.lead",
        index=True,
        ondelete="set null",
        check_company=True,
        readonly=True,
    )
    lead_model = fields.Char(required=True, size=64, readonly=True)
    lead_res_id = fields.Integer(required=True, index=True, readonly=True)
    lead_display_ref = fields.Char(required=True, size=256, readonly=True)
    linked_at = fields.Datetime(
        required=True, default=fields.Datetime.now, index=True, readonly=True
    )

    _sql_constraints = [
        (
            "event_lead_unique",
            "unique(company_id, event_id, lead_id)",
            "This marketing event is already linked to this CRM lead.",
        ),
    ]

    @api.constrains(
        "company_id",
        "event_id",
        "lead_id",
        "lead_model",
        "lead_res_id",
        "lead_display_ref",
    )
    def _check_company_scope(self):
        for link in self:
            if (
                link.lead_model != "crm.lead"
                or link.lead_res_id <= 0
                or not (link.lead_display_ref or "").strip()
                or (link.lead_id and link.lead_id.id != link.lead_res_id)
                or link.event_id.company_id != link.company_id
                or (
                    link.lead_id.company_id
                    and link.lead_id.company_id != link.company_id
                )
            ):
                raise ValidationError(
                    _("Marketing events and CRM leads cannot cross companies.")
                )


class MarketingCrmLeadEquivalence(models.Model):
    """Append-only proof that one CRM lead was merged into another."""

    _name = "marketing.crm.lead.equivalence"
    _description = "Marketing CRM Lead Merge Equivalence"
    _inherit = "marketing.crm.link.immutable.mixin"
    _order = "merged_at desc, id desc"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict", readonly=True
    )
    merge_ref = fields.Char(required=True, size=256, index=True, readonly=True)
    source_lead_model = fields.Char(required=True, size=64, readonly=True)
    source_lead_res_id = fields.Integer(required=True, index=True, readonly=True)
    source_lead_display_ref = fields.Char(required=True, size=256, readonly=True)
    target_lead_id = fields.Many2one(
        "crm.lead",
        index=True,
        ondelete="set null",
        check_company=True,
        readonly=True,
    )
    target_lead_model = fields.Char(required=True, size=64, readonly=True)
    target_lead_res_id = fields.Integer(required=True, index=True, readonly=True)
    target_lead_display_ref = fields.Char(required=True, size=256, readonly=True)
    merged_at = fields.Datetime(
        required=True, default=fields.Datetime.now, index=True, readonly=True
    )

    _sql_constraints = [
        (
            "source_once_unique",
            "unique(company_id, source_lead_model, source_lead_res_id)",
            "This CRM lead has already been merged.",
        ),
        (
            "merge_ref_unique",
            "unique(company_id, merge_ref)",
            "This CRM merge occurrence has already been recorded.",
        ),
        (
            "distinct_leads",
            "check(source_lead_res_id <> target_lead_res_id)",
            "A CRM lead cannot be merged into itself.",
        ),
    ]

    @api.constrains(
        "company_id",
        "source_lead_model",
        "source_lead_res_id",
        "source_lead_display_ref",
        "target_lead_id",
        "target_lead_model",
        "target_lead_res_id",
        "target_lead_display_ref",
    )
    def _check_snapshot_scope(self):
        for equivalence in self:
            if (
                equivalence.source_lead_model != "crm.lead"
                or equivalence.target_lead_model != "crm.lead"
                or equivalence.source_lead_res_id <= 0
                or equivalence.target_lead_res_id <= 0
                or not (equivalence.source_lead_display_ref or "").strip()
                or not (equivalence.target_lead_display_ref or "").strip()
                or (
                    equivalence.target_lead_id
                    and (
                        equivalence.target_lead_id.id != equivalence.target_lead_res_id
                        or (
                            equivalence.target_lead_id.company_id
                            and equivalence.target_lead_id.company_id
                            != equivalence.company_id
                        )
                    )
                )
            ):
                raise ValidationError(_("The CRM merge evidence scope is invalid."))


class MarketingAttributionTouchpoint(models.Model):
    _inherit = "marketing.attribution.touchpoint"

    crm_link_ids = fields.One2many(
        "marketing.attribution.crm.link", "touchpoint_id", readonly=True
    )
    crm_lead_count = fields.Integer(compute="_compute_crm_lead_count")

    def _effective_projection_by_touchpoint(self):
        result = {item.id: False for item in self}
        if not self:
            return result
        pair_by_touchpoint = {
            item.id: (item.company_id.id, item.canonical_key) for item in self
        }
        canonical_pairs = set(pair_by_touchpoint.values())
        effective = self.env["marketing.attribution.effective.touchpoint"].search(
            [
                ("company_id", "in", list({item[0] for item in canonical_pairs})),
                ("canonical_key", "in", list({item[1] for item in canonical_pairs})),
            ]
        )
        effective_by_pair = {
            (item.company_id.id, item.canonical_key): item for item in effective
        }
        for touchpoint_id, pair in pair_by_touchpoint.items():
            result[touchpoint_id] = effective_by_pair.get(pair, False)
        return result

    def _compute_crm_lead_count(self):
        effective_by_touchpoint = self._effective_projection_by_touchpoint()
        for touchpoint in self:
            effective = effective_by_touchpoint[touchpoint.id]
            touchpoint.crm_lead_count = effective.crm_lead_count if effective else 0

    def action_view_crm_leads(self):
        self.ensure_one()
        effective = self._effective_projection_by_touchpoint()[self.id]
        if effective:
            return effective.action_view_crm_leads()
        return self.env["marketing.crm.service"]._lead_action(self.env["crm.lead"])


class MarketingAttributionEffectiveTouchpoint(models.Model):
    _inherit = "marketing.attribution.effective.touchpoint"

    crm_lead_count = fields.Integer(compute="_compute_crm_lead_count")

    def _crm_lead_ids_by_effective_touchpoint(self):
        result = {item.id: set() for item in self}
        if not self:
            return result
        links = (
            self.env["marketing.attribution.crm.effective.link"]
            .sudo()
            .search([("touchpoint_id", "in", self.ids)])
        )
        links.invalidate_recordset(["touchpoint_id", "lead_id"])
        for link in links:
            if link.lead_id:
                result[link.touchpoint_id.id].add(link.lead_id.id)
        return result

    def _compute_crm_lead_count(self):
        leads_by_touchpoint = self._crm_lead_ids_by_effective_touchpoint()
        for touchpoint in self:
            touchpoint.crm_lead_count = len(leads_by_touchpoint[touchpoint.id])

    def action_view_crm_leads(self):
        self.ensure_one()
        lead_ids = self._crm_lead_ids_by_effective_touchpoint()[self.id]
        leads = self.env["crm.lead"].browse(sorted(lead_ids))
        return self.env["marketing.crm.service"]._lead_action(leads)


class MarketingBusinessEvent(models.Model):
    _inherit = "marketing.business.event"

    crm_link_ids = fields.One2many(
        "marketing.business.event.crm.link", "event_id", readonly=True
    )
    crm_lead_count = fields.Integer(compute="_compute_crm_lead_count")

    def _compute_crm_lead_count(self):
        grouped = self.env["marketing.business.event.crm.link"].read_group(
            [("event_id", "in", self.ids), ("lead_id", "!=", False)],
            ["event_id"],
            ["event_id"],
        )
        counts = {row["event_id"][0]: row["event_id_count"] for row in grouped}
        for event in self:
            event.crm_lead_count = counts.get(event.id, 0)

    def action_view_crm_leads(self):
        self.ensure_one()
        leads = self.crm_link_ids.mapped("lead_id")
        return self.env["marketing.crm.service"]._lead_action(leads)

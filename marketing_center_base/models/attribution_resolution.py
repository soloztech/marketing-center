from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

ASSET_RESOLUTION_WRITE_TOKEN = object()

RESOLUTION_STATES = [
    ("resolved", "Resolved"),
    ("unresolved", "Unresolved"),
    ("ambiguous", "Ambiguous"),
    ("unsupported", "Unsupported"),
]

RESOLUTION_TARGET_KINDS = [
    ("source", "Marketing Source"),
    ("entity", "External Entity"),
    ("unknown", "Unknown"),
]


class MarketingAttributionAssetResolution(models.Model):
    """Rebuildable technical projection from a touchpoint asset reference.

    This table is deliberately mutable.  It is an index over the immutable
    attribution ledger, not evidence and not a statement of causality.
    """

    _name = "marketing.attribution.asset.resolution"
    _description = "Marketing Attribution Asset Resolution"
    _order = "last_attempted_at desc, canonical_key, asset_namespace, id"
    _rec_name = "asset_namespace"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        ondelete="restrict",
        readonly=True,
    )
    touchpoint_id = fields.Many2one(
        "marketing.attribution.touchpoint",
        required=True,
        index=True,
        ondelete="cascade",
        check_company=True,
        readonly=True,
        help="Physical immutable revision currently selected by the projection.",
    )
    canonical_key = fields.Char(
        required=True,
        size=64,
        index=True,
        readonly=True,
    )
    asset_namespace = fields.Char(
        required=True,
        size=128,
        index=True,
        readonly=True,
    )
    asset_value = fields.Char(required=True, size=512, readonly=True)
    resolver_version = fields.Integer(required=True, default=1, readonly=True)
    target_kind = fields.Selection(
        RESOLUTION_TARGET_KINDS,
        required=True,
        default="unknown",
        index=True,
        readonly=True,
    )
    provider_key = fields.Char(size=64, index=True, readonly=True)
    service_key = fields.Char(size=128, index=True, readonly=True)
    mapped_entity_type = fields.Char(size=128, index=True, readonly=True)
    canonical_external_ref = fields.Char(size=1024, index=True, readonly=True)
    source_id = fields.Many2one(
        "marketing.center.source",
        index=True,
        ondelete="cascade",
        check_company=True,
        readonly=True,
    )
    entity_id = fields.Many2one(
        "marketing.center.external.entity",
        index=True,
        ondelete="cascade",
        check_company=True,
        readonly=True,
    )
    state = fields.Selection(
        RESOLUTION_STATES,
        required=True,
        index=True,
        readonly=True,
    )
    reason = fields.Char(required=True, size=128, index=True, readonly=True)
    source_candidate_count = fields.Integer(required=True, default=0, readonly=True)
    entity_candidate_count = fields.Integer(required=True, default=0, readonly=True)
    attempt_count = fields.Integer(required=True, default=1, readonly=True)
    first_attempted_at = fields.Datetime(required=True, readonly=True)
    last_attempted_at = fields.Datetime(required=True, index=True, readonly=True)
    resolved_at = fields.Datetime(index=True, readonly=True)

    _sql_constraints = [
        (
            "canonical_ns_uniq",
            "unique(company_id, canonical_key, asset_namespace)",
            "An asset namespace can have only one current resolution per touchpoint.",
        ),
        (
            "attempt_count_positive",
            "check(attempt_count > 0)",
            "The asset-resolution attempt count must be positive.",
        ),
        (
            "candidate_nonnegative",
            "check(source_candidate_count >= 0 AND entity_candidate_count >= 0)",
            "Asset-resolution candidate counts cannot be negative.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        self._check_internal_write()
        return super().create(vals_list)

    def write(self, values):
        self._check_internal_write()
        return super().write(values)

    def unlink(self):
        self._check_internal_write()
        return super().unlink()

    def _check_internal_write(self):
        if (
            self.env.context.get("marketing_asset_resolution_write_token")
            is not ASSET_RESOLUTION_WRITE_TOKEN
        ):
            raise AccessError(
                _("Asset resolutions are maintained only by the resolver service.")
            )

    @api.model
    def _cron_backfill(self, limit=200, retry_after_minutes=5):
        return self.env[
            "marketing.attribution.asset.resolution.service"
        ]._cron_backfill(limit=limit, retry_after_minutes=retry_after_minutes)

    @api.constrains(
        "company_id",
        "touchpoint_id",
        "source_id",
        "entity_id",
        "target_kind",
        "state",
    )
    def _check_resolution_scope(self):
        for resolution in self:
            if resolution.touchpoint_id.company_id != resolution.company_id:
                raise ValidationError(
                    _("The asset resolution and touchpoint must share a company.")
                )
            if resolution.source_id and (
                resolution.source_id.company_id != resolution.company_id
            ):
                raise ValidationError(
                    _("The asset resolution and source must share a company.")
                )
            if resolution.entity_id and (
                not resolution.source_id
                or resolution.entity_id.source_id != resolution.source_id
            ):
                raise ValidationError(
                    _("The resolved entity must belong to the resolved source.")
                )
            if resolution.state == "resolved" and not resolution.source_id:
                raise ValidationError(
                    _("A resolved asset reference must identify a source.")
                )
            if (
                resolution.state == "resolved"
                and resolution.target_kind == "entity"
                and not resolution.entity_id
            ):
                raise ValidationError(
                    _("A resolved entity reference must identify an entity.")
                )


class MarketingAttributionTouchpointResolution(models.Model):
    _inherit = "marketing.attribution.touchpoint"

    asset_resolution_ids = fields.One2many(
        "marketing.attribution.asset.resolution",
        "touchpoint_id",
        string="Technical Asset Resolutions",
        readonly=True,
        groups="marketing_center_base.group_marketing_center_admin",
    )

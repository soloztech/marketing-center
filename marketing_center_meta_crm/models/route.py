from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .tokens import MARKETING_META_CRM_MIGRATION_TOKEN


class MarketingCenterMetaLeadRoute(models.Model):
    _inherit = "marketing.center.meta.lead.route"

    crm_auto_create_lead = fields.Boolean(
        string="Automatically create CRM leads",
        default=False,
        help=(
            "Create one CRM lead only after Meta returned and authenticated the full "
            "Lead Ads submission. Webhook hints and ad identifiers never create leads."
        ),
    )
    crm_team_id = fields.Many2one(
        "crm.team",
        string="CRM sales team",
        check_company=True,
        ondelete="restrict",
    )
    crm_user_id = fields.Many2one(
        "res.users",
        string="CRM salesperson",
        ondelete="restrict",
        domain="[('share', '=', False), ('active', '=', True)]",
    )
    crm_lead_type = fields.Selection(
        [
            ("native", "Follow CRM settings"),
            ("lead", "Lead"),
            ("opportunity", "Opportunity"),
        ],
        string="CRM record type",
        required=True,
        default="native",
    )
    crm_history_policy = fields.Selection(
        [("since", "From a date"), ("all", "All submissions")],
        string="CRM submission history",
        required=True,
        default="since",
    )
    crm_create_from = fields.Datetime(
        string="Create CRM records from",
        copy=False,
        help="Use the submission creation time authenticated by Meta.",
    )
    crm_tag_ids = fields.Many2many(
        "crm.tag",
        "marketing_meta_lead_route_crm_tag_rel",
        "route_id",
        "tag_id",
        string="CRM tags",
    )
    crm_lead_title_prefix = fields.Char(
        string="Lead title prefix",
        required=True,
        default="Meta Lead Ads",
        size=128,
    )
    crm_projection_ids = fields.One2many(
        "marketing.center.meta.crm.projection", "route_id", readonly=True
    )

    @api.model
    def _crm_configuration_fields(self):
        return {
            "crm_auto_create_lead",
            "crm_team_id",
            "crm_user_id",
            "crm_lead_type",
            "crm_history_policy",
            "crm_create_from",
            "crm_tag_ids",
            "crm_lead_title_prefix",
        }

    @api.model
    def _check_crm_configuration_access(self):
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_admin"
        ):
            raise AccessError(
                _("Only Marketing Center administrators can configure CRM projection.")
            )

    @api.model_create_multi
    def create(self, vals_list):
        # Materialize effective defaults before checking activation, including
        # defaults supplied by an import/action context.
        defaults = self.default_get(list(self._crm_configuration_fields()))
        prepared = []
        for incoming in vals_list:
            values = dict(incoming)
            effective = dict(defaults, **values)
            if (
                self._crm_configuration_fields().intersection(values)
                or any(
                    "default_%s" % name in self.env.context
                    for name in self._crm_configuration_fields()
                )
                or effective.get("crm_auto_create_lead")
            ):
                self._check_crm_configuration_access()
            if (
                effective.get("crm_auto_create_lead")
                and effective.get("crm_history_policy") == "since"
                and not effective.get("crm_create_from")
            ):
                values["crm_create_from"] = fields.Datetime.now()
            prepared.append(values)
        return super().create(prepared)

    @api.onchange("crm_auto_create_lead", "crm_history_policy")
    def _onchange_crm_history_policy(self):
        for route in self:
            if (
                route.crm_auto_create_lead
                and route.crm_history_policy == "since"
                and not route.crm_create_from
            ):
                route.crm_create_from = fields.Datetime.now()

    def write(self, values):
        crm_change = bool(self._crm_configuration_fields().intersection(values))
        if not crm_change:
            return super().write(values)
        self._check_crm_configuration_access()
        # Normalize separately: a multi-route write may activate one route while
        # reactivating another with an established historical boundary.
        for route in self.sorted("id"):
            route_values = dict(values)
            enabled = route_values.get(
                "crm_auto_create_lead", route.crm_auto_create_lead
            )
            policy = route_values.get("crm_history_policy", route.crm_history_policy)
            cutoff = route_values.get("crm_create_from", route.crm_create_from)
            activating = enabled and not route.crm_auto_create_lead
            switching_to_since = (
                enabled and policy == "since" and route.crm_history_policy != "since"
            )
            if (
                policy == "since"
                and not cutoff
                and (activating or switching_to_since)
            ):
                route_values["crm_create_from"] = fields.Datetime.now()
            super(MarketingCenterMetaLeadRoute, route).write(route_values)
        self.flush_recordset(list(self._crm_configuration_fields()))
        if (
            self.env.context.get("marketing_meta_crm_migration")
            is not MARKETING_META_CRM_MIGRATION_TOKEN
        ):
            self._skip_ineligible_crm_projections()
            self._enqueue_crm_company_backfill()
        return True

    def _lock_crm_configuration(self):
        """Keep job decisions stable until CRM creation commits.

        Both workers and configuration writers lock the route before locking
        projections, so a changed policy cannot race an accepted old job.
        """

        self.ensure_one()
        configuration_fields = list(self._crm_configuration_fields())
        self.flush_recordset(configuration_fields)
        self.env.cr.execute(
            "SELECT id FROM marketing_center_meta_lead_route "
            "WHERE id = %s FOR SHARE",
            [self.id],
        )
        self.invalidate_recordset(configuration_fields)
        return self

    def _crm_record_type(self):
        self.ensure_one()
        if self.crm_lead_type != "native":
            return self.crm_lead_type
        if self.crm_team_id:
            use_leads = self.crm_team_id.use_leads
        else:
            use_leads = (
                self.env["res.users"]
                .with_user(SUPERUSER_ID)
                .browse(SUPERUSER_ID)
                .has_group("crm.group_use_lead")
            )
        return "lead" if use_leads else "opportunity"

    def _crm_accepts_submission(self, submission):
        self.ensure_one()
        submission.ensure_one()
        if (
            not self.crm_auto_create_lead
            or submission.route_id != self
            or submission.company_id != self.company_id
            or submission.state != "ingested"
            or not submission.touchpoint_id
        ):
            return False
        if self.crm_history_policy == "all":
            return True
        return bool(
            self.crm_history_policy == "since"
            and self.crm_create_from
            and submission.provider_created_at
            and submission.provider_created_at >= self.crm_create_from
        )

    def _skip_ineligible_crm_projections(self):
        Projection = self.env["marketing.center.meta.crm.projection"].sudo()
        after_id = 0
        while True:
            batch = Projection.search(
                [
                    ("route_id", "in", self.ids),
                    ("id", ">", after_id),
                    ("state", "!=", "done"),
                ],
                order="id",
                limit=200,
            )
            if not batch:
                break
            for projection in batch:
                if not projection.route_id._crm_accepts_submission(
                    projection.submission_id
                ):
                    projection._mark_skipped()
            after_id = batch[-1].id

    def _enqueue_crm_company_backfill(self):
        enabled = self.filtered("crm_auto_create_lead")
        for company in enabled.mapped("company_id"):
            route_ids = tuple(
                sorted(enabled.filtered(lambda route: route.company_id == company).ids)
            )
            company.sudo().with_context(
                allowed_company_ids=[company.id]
            ).with_company(company)._enqueue_marketing_meta_crm_backfill(
                route_ids=route_ids
            )

    @api.constrains(
        "company_id",
        "crm_team_id",
        "crm_user_id",
        "crm_lead_title_prefix",
        "crm_auto_create_lead",
        "crm_history_policy",
        "crm_create_from",
    )
    def _check_crm_projection_configuration(self):
        for route in self:
            if (
                route.crm_auto_create_lead
                and route.crm_history_policy == "since"
                and not route.crm_create_from
            ):
                raise ValidationError(
                    _("A starting date is required for automatic CRM creation.")
                )
            if route.crm_team_id and route.crm_team_id.company_id not in (
                False,
                route.company_id,
            ):
                raise ValidationError(
                    _("The CRM sales team must belong to the Lead Ads company.")
                )
            if (
                route.crm_user_id
                and route.company_id not in route.crm_user_id.company_ids
            ):
                raise ValidationError(
                    _("The CRM salesperson must have access to the Lead Ads company.")
                )
            if not (route.crm_lead_title_prefix or "").strip():
                raise ValidationError(_("The CRM lead title prefix is required."))

    def action_enqueue_crm_backfill(self):
        self._check_admin()
        self.check_access_rights("read")
        self.check_access_rule("read")
        self._skip_ineligible_crm_projections()
        self._enqueue_crm_company_backfill()
        return True

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError


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
        [("lead", "Lead"), ("opportunity", "Opportunity")],
        string="CRM record type",
        required=True,
        default="lead",
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
            "crm_tag_ids",
            "crm_lead_title_prefix",
        }

    def write(self, values):
        crm_change = bool(self._crm_configuration_fields().intersection(values))
        if crm_change and not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_admin"
        ):
            raise AccessError(
                _("Only Marketing Center administrators can configure CRM projection.")
            )
        result = super().write(values)
        if crm_change:
            enabled = self.filtered("crm_auto_create_lead")
            disabled = self - enabled
            disabled.crm_projection_ids.filtered(
                lambda projection: projection.state != "done"
            ).sudo()._internal_write(
                {
                    "state": "skipped",
                    "queue_job_uuid": False,
                    "last_error_class": False,
                    "last_error_message": False,
                }
            )
            self.env["marketing.center.meta.crm.service"].sudo()._ensure_and_enqueue(
                enabled.mapped("submission_ids").filtered(
                    lambda submission: submission.state == "ingested"
                    and submission.touchpoint_id
                ),
                retry_terminal=True,
            )
        return result

    @api.constrains(
        "company_id",
        "crm_team_id",
        "crm_user_id",
        "crm_lead_title_prefix",
    )
    def _check_crm_projection_configuration(self):
        for route in self:
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
        submissions = self.filtered("crm_auto_create_lead").mapped("submission_ids")
        submissions = submissions.filtered(
            lambda submission: submission.state == "ingested"
            and submission.touchpoint_id
        )
        self.env["marketing.center.meta.crm.service"].sudo()._ensure_and_enqueue(
            submissions, retry_terminal=True
        )
        return True

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .tokens import WEBSITE_CRM_WRITE_TOKEN


class MarketingWebsiteCrmCorrelation(models.Model):
    _name = "marketing.website.crm.correlation"
    _description = "Immutable Website Form CRM Correlation"
    _order = "linked_at desc, id desc"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company", required=True, readonly=True, index=True, ondelete="restrict"
    )
    event_id = fields.Many2one(
        "marketing.web.ingress.event",
        required=True,
        readonly=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    action_id = fields.Many2one(
        "marketing.website.action",
        required=True,
        readonly=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    touchpoint_id = fields.Many2one(
        "marketing.attribution.touchpoint",
        required=True,
        readonly=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    lead_id = fields.Many2one(
        "crm.lead",
        readonly=True,
        index=True,
        ondelete="set null",
        check_company=True,
    )
    lead_model = fields.Char(required=True, readonly=True, size=64)
    lead_res_id = fields.Integer(required=True, readonly=True, index=True)
    lead_display_ref = fields.Char(required=True, readonly=True, size=256)
    assertion_id = fields.Many2one(
        "marketing.attribution.crm.link",
        required=True,
        readonly=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    linked_at = fields.Datetime(
        required=True, readonly=True, index=True, default=fields.Datetime.now
    )

    _sql_constraints = [
        (
            "event_unique",
            "unique(event_id)",
            "A Website form event can be correlated with only one CRM lead.",
        ),
        (
            "assertion_unique",
            "unique(assertion_id)",
            "A Website form correlation must own one CRM assertion.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("marketing_website_crm_write_token")
            is not WEBSITE_CRM_WRITE_TOKEN
        ):
            raise AccessError(_("Website CRM correlations are managed internally."))
        return super().create(vals_list)

    def write(self, _values):  # pylint: disable=method-required-super
        raise AccessError(_("Website CRM correlations cannot be edited."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Website CRM correlations cannot be deleted."))

    @api.constrains(
        "company_id",
        "event_id",
        "action_id",
        "touchpoint_id",
        "lead_id",
        "lead_model",
        "lead_res_id",
        "lead_display_ref",
        "assertion_id",
    )
    def _check_scope(self):
        for record in self:
            asset_refs = record.touchpoint_id.asset_refs_json or {}
            companies = {
                record.event_id.company_id,
                record.action_id.company_id,
                record.touchpoint_id.company_id,
                record.assertion_id.company_id,
            }
            lead_company = (
                record.lead_id.marketing_event_company_id or record.lead_id.company_id
            )
            if lead_company:
                companies.add(lead_company)
            if companies != {record.company_id}:
                raise ValidationError(
                    _("Website CRM correlations cannot cross companies.")
                )
            if (
                record.event_id.touchpoint_id != record.touchpoint_id
                or record.event_id.endpoint_id
                != record.action_id.binding_id.endpoint_id
                or not isinstance(asset_refs, dict)
                or asset_refs.get("web.action") != record.action_id.public_ref
                or asset_refs.get("web.model") != "crm.lead"
                or record.lead_model != "crm.lead"
                or record.lead_res_id <= 0
                or not (record.lead_display_ref or "").strip()
                or (record.lead_id and record.lead_id.id != record.lead_res_id)
                or record.assertion_id.touchpoint_id != record.touchpoint_id
                or record.assertion_id.lead_model != record.lead_model
                or record.assertion_id.lead_res_id != record.lead_res_id
                or record.assertion_id.lead_display_ref != record.lead_display_ref
                or (record.lead_id and record.assertion_id.lead_id != record.lead_id)
            ):
                raise ValidationError(
                    _("The Website CRM correlation evidence is inconsistent.")
                )

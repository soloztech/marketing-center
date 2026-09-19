from odoo import _, fields, models
from odoo.exceptions import AccessError

from odoo.addons.marketing_center_website.models.website_visitor import GEO_FIELDS

from .tokens import WEBSITE_NATIVE_SUBMISSION_TOKEN as _TOKEN


class CrmLead(models.Model):
    _inherit = "crm.lead"

    marketing_ip_address = fields.Char(
        string="IP no envio",
        size=45,
        readonly=True,
        copy=False,
        groups="base.group_user",
    )
    marketing_ip_observed_at = fields.Datetime(
        string="Data da observação", readonly=True, copy=False, groups="base.group_user"
    )
    marketing_geo_country_id = fields.Many2one(
        "res.country",
        string="País estimado",
        readonly=True,
        copy=False,
        groups="base.group_user",
    )
    marketing_geo_state_id = fields.Many2one(
        "res.country.state",
        string="Estado estimado",
        readonly=True,
        copy=False,
        groups="base.group_user",
    )
    marketing_geo_city = fields.Char(
        string="Cidade estimada",
        size=128,
        readonly=True,
        copy=False,
        groups="base.group_user",
    )
    marketing_geo_timezone = fields.Char(
        string="Fuso estimado",
        size=64,
        readonly=True,
        copy=False,
        groups="base.group_user",
    )
    marketing_geo_source = fields.Selection(
        [("cloudflare", "Cloudflare"), ("geoip", "GeoIP do Odoo")],
        string="Origem da localização",
        readonly=True,
        copy=False,
        groups="base.group_user",
    )

    def write(self, values):
        if (
            set(GEO_FIELDS).intersection(values)
            and self.env.context.get("marketing_native_submission_token") is not _TOKEN
        ):
            raise AccessError(_("A observação de IP do envio não pode ser reescrita."))
        return super().write(values)

    def _erase_marketing_ip_observation(self):
        """Erase this copy and linked visitors in the existing erasure transaction."""
        values = dict.fromkeys(GEO_FIELDS, False)
        self.visitor_ids.sudo().write(values)
        self.with_context(marketing_native_submission_token=_TOKEN).write(values)

# Keep component override order and super() behavior during the fusion.
# pylint: disable=consider-merging-classes-inherited
import datetime

from odoo import _, api, fields, models
from odoo.tools.misc import formatLang

LEAD_WINDOW_DAYS = 30


class MarketingCenterCampaignBoardCrm(models.Model):
    _inherit = "marketing.center.external.entity"

    campaign_lead_label = fields.Char(
        compute="_compute_campaign_lead_label", string="Leads"
    )

    @api.depends("native_utm_campaign_id")
    @api.depends_context("lang", "uid", "allowed_company_ids")
    def _compute_campaign_lead_label(self):
        """Leads of the linked UTM campaign that the current user may read.

        Counted in the user's own security context: salesperson and company
        record rules apply, so the number is "visible to you", not a total.
        """

        leads = self.env["crm.lead"]
        if not leads.check_access_rights("read", raise_exception=False):
            for entity in self:
                entity.campaign_lead_label = _("No CRM access")
            return
        linked = self.filtered("native_utm_campaign_id")
        counts = {}
        if linked:
            since = fields.Datetime.now() - datetime.timedelta(days=LEAD_WINDOW_DAYS)
            # Lost leads are archived by the native CRM; they were still acquired.
            groups = leads.with_context(active_test=False).read_group(
                [
                    ("campaign_id", "in", linked.native_utm_campaign_id.ids),
                    ("create_date", ">=", since),
                ],
                ["campaign_id"],
                ["campaign_id"],
            )
            counts = {
                group["campaign_id"][0]: group["campaign_id_count"]
                for group in groups
                if group["campaign_id"]
            }
        for entity in self:
            campaign = entity.native_utm_campaign_id
            if not campaign:
                entity.campaign_lead_label = _("No linked UTM campaign")
                continue
            entity.campaign_lead_label = _(
                "%(count)s leads visible to you in %(days)s days"
            ) % {
                "count": formatLang(self.env, counts.get(campaign.id, 0), digits=0),
                "days": LEAD_WINDOW_DAYS,
            }

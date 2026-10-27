"""Freeze acquisition at a meaningful native Website WhatsApp click."""

from urllib.parse import urlsplit

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from odoo.addons.marketing_center_website.services.acquisition import (
    resolve_acquisition,
    safe_page,
    utc_iso,
)


class MarketingWebsiteWhatsAppCapture(models.AbstractModel):
    _name = "marketing.website.whatsapp.capture"
    _description = "Native Website WhatsApp acquisition capture"

    @api.model
    def _snapshot(self, action, origin, referrer, visitor=None, cookies=None, now=None):
        action.ensure_one()
        now = now or fields.Datetime.now()
        website = action.website_id
        page_url = safe_page(referrer, origin)
        if (
            not page_url
            or website._whatsapp_handoff_action(urlsplit(page_url).path) != action
        ):
            raise AccessError(_("A página não corresponde à ação WhatsApp."))

        selected = resolve_acquisition(
            self.env,
            website,
            origin,
            referrer,
            now,
            visitor=visitor,
            cookies=cookies,
            purpose="whatsapp",
        )
        acquisition = dict(selected["values"])
        if selected["acquired_at"]:
            acquisition["acquisition_at"] = utc_iso(selected["acquired_at"])
        acquisition["acquisition_provenance"] = selected["provenance"]
        return {
            "page_url": selected["page_url"],
            "landing_url": selected["landing_url"],
            "acquisition": acquisition,
            "track": selected["track"],
            "visit_at": selected["visit_at"] or False,
        }

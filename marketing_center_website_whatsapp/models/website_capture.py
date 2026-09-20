"""Freeze acquisition at a meaningful native Website WhatsApp click."""

import datetime
import re
from urllib.parse import unquote, urlsplit

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from odoo.addons.marketing_center_website_crm.models.native_submission import (
    acquisition_values, safe_page, utc_iso,
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
        if (not page_url or website._whatsapp_handoff_action(urlsplit(page_url).path) != action):
            raise AccessError(_("A página não corresponde à ação WhatsApp."))

        landing_url = page_url
        acquisition = acquisition_values(referrer)
        chosen_track = self.env["website.track"]
        acquired_at = None
        if visitor and visitor.exists():
            tracks = self.env["website.track"].sudo().search([
                ("visitor_id", "=", visitor.id),
                ("visit_datetime", "<=", now),
                ("visit_datetime", ">=", now - datetime.timedelta(hours=24)),
            ], order="visit_datetime desc, id desc", limit=200).filtered(
                lambda track: safe_page(track.url, origin)
                and (not track.page_id or not track.page_id.website_id
                     or track.page_id.website_id == website)
            )
            anchor = next((track for track in tracks
                           if safe_page(track.url, origin) == page_url
                           and (not acquisition or acquisition_values(track.url) == acquisition)), None)
            if anchor:
                acquired_at = anchor.visit_datetime
                chosen_track = anchor
                if not acquisition:
                    preceding = tracks.filtered(
                        lambda track: (track.visit_datetime, track.id)
                        <= (anchor.visit_datetime, anchor.id)
                    )
                    chosen = next((track for track in preceding if acquisition_values(track.url)), None)
                    if chosen:
                        acquisition = acquisition_values(chosen.url)
                        landing_url = safe_page(chosen.url, origin)
                        acquired_at = chosen.visit_datetime
                        chosen_track = chosen
        # A partial click tuple owns its origin; old UTM cookies never fill it.
        if not acquisition:
            for suffix in ("source", "medium", "campaign"):
                value = unquote((cookies or {}).get("odoo_utm_" + suffix, ""))
                if value and len(value) <= 512 and not re.search(r"[\x00-\x1f\x7f]", value):
                    acquisition["utm_" + suffix] = value
        if acquired_at:
            acquisition["acquisition_at"] = utc_iso(acquired_at)
        return {
            "page_url": page_url, "landing_url": landing_url,
            "acquisition": acquisition, "track": chosen_track,
        }

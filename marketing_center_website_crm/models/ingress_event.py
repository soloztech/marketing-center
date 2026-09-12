from odoo import models

from .intent import _WEBSITE_CRM_RETENTION_TOKEN


class MarketingWebIngressEvent(models.Model):
    _inherit = "marketing.web.ingress.event"

    def _erase_related_private_values(self, *, now):
        result = super()._erase_related_private_values(now=now)
        for event in self:
            intents = (
                self.env["marketing.website.crm.intent"]
                .sudo()
                .search(
                    [
                        ("company_id", "=", event.company_id.id),
                        ("endpoint_id", "=", event.endpoint_id.id),
                        ("event_key_hash", "=", event.event_key_hash),
                        ("privacy_erased_at", "=", False),
                    ]
                )
            )
            intents._erase_session_values(token=_WEBSITE_CRM_RETENTION_TOKEN, now=now)
        return result

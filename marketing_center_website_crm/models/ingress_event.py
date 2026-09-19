from odoo import models

from .intent import _WEBSITE_CRM_RETENTION_TOKEN
from .tokens import WEBSITE_NATIVE_SUBMISSION_TOKEN as _TOKEN


class MarketingWebIngressEvent(models.Model):
    _inherit = "marketing.web.ingress.event"

    def _erase_related_private_values(self, *, now):
        result = super()._erase_related_private_values(now=now)
        for event in self:
            leads = (
                self.env["marketing.website.crm.correlation"]
                .sudo()
                .search(
                    [
                        ("event_id", "=", event.id),
                    ]
                )
                .mapped("lead_id")
            )
            leads._erase_marketing_ip_observation()
            leads.with_context(marketing_native_submission_token=_TOKEN).write(
                {
                    "marketing_native_snapshot": False,
                }
            )
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

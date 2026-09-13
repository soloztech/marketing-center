from dataclasses import replace
from urllib.parse import unquote, urlsplit

from odoo import _, api, models
from odoo.exceptions import AccessError
from odoo.addons.marketing_center_base.services.dto import PrivacySnapshotDTO


class MarketingWebIngressService(models.AbstractModel):
    _inherit = "marketing.web.ingress.service"

    @api.model
    def _ingest_payload(self, endpoint, payload, **kwargs):
        if endpoint.privacy_legal_basis_code == "consent":
            decision = self.env["marketing.website.consent"]._current(endpoint)
            if not decision:
                raise AccessError(_("Individual consent is required."))
            path = unquote(urlsplit(payload.get("landing_url", "")).path).lower()
            if any(
                path == prefix or path.startswith(prefix + "/")
                for prefix in (
                    "/auth",
                    "/marketing",
                    "/my",
                    "/portal",
                    "/web",
                    "/website",
                )
            ):
                raise AccessError(
                    _("Technical pages cannot create marketing evidence.")
                )
        return super()._ingest_payload(endpoint, payload, **kwargs)

    @api.model
    def _touchpoint_dto(self, endpoint, *args, **kwargs):
        dto = super()._touchpoint_dto(endpoint, *args, **kwargs)
        if endpoint.privacy_legal_basis_code != "consent":
            return dto
        decision = self.env["marketing.website.consent"]._current(endpoint)
        if not decision:
            raise AccessError(_("Individual consent is required."))
        return replace(
            dto,
            privacy=PrivacySnapshotDTO(
                policy_version=decision.policy_version,
                notice_version=decision.notice_version,
                legal_basis_code="consent",
                consent_state="granted",
                decision_source="website.cookiebar.v1",
                decided_at=decision.decided_at,
            ),
        )

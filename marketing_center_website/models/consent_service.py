from dataclasses import replace
from urllib.parse import unquote, urlsplit

from odoo import _, api, models
from odoo.exceptions import AccessError
from odoo.http import request
from odoo.addons.marketing_center_base.services.dto import PrivacySnapshotDTO


class MarketingWebIngressService(models.AbstractModel):
    _inherit = "marketing.web.ingress.service"

    @api.model
    def _ingest_payload(self, endpoint, payload, **kwargs):
        if endpoint.privacy_legal_basis_code == "consent":
            if (
                endpoint._requires_individual_consent()
                and not self.env["marketing.website.consent"]._current(endpoint)
            ):
                raise AccessError(_("Individual consent is required."))
            if request and getattr(request, "httprequest", None):
                if request.session.uid:
                    raise AccessError(_("Authenticated sessions cannot create marketing evidence."))
                if (
                    request.httprequest.path.startswith(
                        ("/marketing/web-ingress/", "/marketing/website-action/")
                    )
                    and request.httprequest.scheme != "https"
                ):
                    raise AccessError(_("Website marketing capture requires HTTPS."))
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
        if endpoint._tracking_test_mode():
            # The operator enabled capture, not consent. Record this distinction
            # durably without inventing a legal basis or an accepted cookie.
            privacy = (
                PrivacySnapshotDTO(
                    policy_version=decision.policy_version,
                    notice_version=decision.notice_version,
                    legal_basis_code=endpoint.privacy_legal_basis_code,
                    consent_state="granted",
                    decision_source="operator.test_override",
                    decided_at=decision.decided_at,
                )
                if decision
                else replace(
                    dto.privacy,
                    consent_state="unknown",
                    decision_source="operator.test_override",
                    decided_at=None,
                )
            )
            return replace(
                dto,
                privacy=privacy,
                extensions={**dto.extensions, "web_ingress.tracking_test_mode": True},
            )
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

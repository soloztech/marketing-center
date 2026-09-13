from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class MarketingWebIngressEndpoint(models.Model):
    _inherit = "marketing.web.ingress.endpoint"

    consent_ttl_days = fields.Integer(
        default=999,
        required=True,
        help="Technical validity of an individual consent receipt; independent of attribution-history retention.",
    )

    @api.constrains("consent_ttl_days")
    def _check_consent_ttl(self):
        if any(not 1 <= endpoint.consent_ttl_days <= 3650 for endpoint in self):
            raise ValidationError(
                _("Consent receipt validity must be between 1 and 3650 days.")
            )

    @api.model
    def _privacy_policy_fields(self):
        return super()._privacy_policy_fields() | {"consent_ttl_days"}

    def write(self, values):
        if self and "active" in values and not values["active"]:
            self.check_access_rights("write")
            self.check_access_rule("write")
            self.env.cr.execute(
                "SELECT id FROM marketing_web_ingress_endpoint "
                "WHERE id IN %s ORDER BY id FOR UPDATE",
                [tuple(sorted(self.ids))],
            )
            binding = (
                self.env["marketing.website.ingress.binding"]
                .sudo()
                .search(
                    [
                        ("active", "=", True),
                        ("endpoint_id", "in", self.ids),
                    ],
                    limit=1,
                )
            )
            if binding:
                raise ValidationError(
                    _(
                        "Archive the active website ingress binding before "
                        "deactivating its endpoint."
                    )
                )
        return super().write(values)

    def _privacy_policy_configured(self):
        self.ensure_one()
        if self.privacy_legal_basis_code == "consent":
            return bool(
                self.capture_purpose
                and (self.privacy_policy_version or "").strip()
                and (self.privacy_notice_version or "").strip()
                and (self.privacy_policy_justification or "").strip()
                and (
                    self.retention_mode == "manual"
                    or self.identifier_retention_days > 0
                )
            )
        return super()._privacy_policy_configured()

    def _capture_policy_allows(self):
        allowed = super()._capture_policy_allows()
        if allowed and self.privacy_legal_basis_code == "consent":
            return bool(self.env["marketing.website.consent"]._current(self))
        return allowed

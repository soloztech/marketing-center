from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class MarketingWebIngressEndpoint(models.Model):
    _inherit = "marketing.web.ingress.endpoint"

    website_tracking_policy = fields.Selection(
        [("individual_consent", "Escolha individual de cookies"),
         ("informational_notice", "Aviso informativo com Prosseguir")],
        default="individual_consent", required=True,
        help="A política informativa é uma decisão documentada do operador. "
        "Ela não representa consentimento do visitante.",
    )

    def _informational_notice(self):
        self.ensure_one()
        return self.website_tracking_policy == "informational_notice"

    def _requires_individual_consent(self):
        self.ensure_one()
        return self.privacy_legal_basis_code == "consent" and not self._informational_notice()

    @api.constrains("website_tracking_policy", "privacy_legal_basis_code", "capture_enabled")
    def _check_website_tracking_policy(self):
        for endpoint in self:
            if endpoint._informational_notice() and (
                (endpoint.privacy_legal_basis_code or "").lower() in {"consent", "granted"}
            ):
                raise ValidationError(_("O aviso informativo não representa consentimento. "
                                        "Não use consentimento como base dessa política."))
            if endpoint.capture_enabled and not endpoint._privacy_policy_configured():
                raise ValidationError(_("Documente a finalidade, as versões do aviso e da política, "
                                        "a justificativa e a retenção antes de habilitar a captura."))

    def _activate_informational_notice(self, *, website_id, binding_id, company_id,
                                       expected_revision, policy_version, notice_version,
                                       justification):
        """Explicit reviewed deployment operation; never called on upgrade or HTTP.

        The caller first snapshots/copies legacy notice translations with the old
        registry. Only this exact endpoint/binding/site/company is promoted; an
        obsolete global test allowlist does not authorize permanent capture.
        """
        self.ensure_one()
        if any(type(value) is not int or value <= 0 for value in
               (website_id, binding_id, company_id, expected_revision)):
            raise ValidationError(_("Informe as identidades e a revisão verificadas."))
        self.check_access_rights("write")
        self.check_access_rule("write")
        binding = self.env["marketing.website.ingress.binding"].browse(binding_id).exists()
        if not binding:
            raise ValidationError(_("O vínculo do site não está disponível."))
        binding._fence_effective_configuration()
        self.invalidate_recordset()
        if (binding.endpoint_id != self or binding.website_id.id != website_id
                or binding.company_id.id != company_id or self.company_id.id != company_id
                or not binding.active or not self.active
                or self.config_revision != expected_revision
                or not binding.website_id.cookies_bar):
            raise ValidationError(_("A configuração mudou ou não corresponde ao site revisado."))
        if not all(isinstance(value, str) and value.strip()
                   for value in (policy_version, notice_version, justification)):
            raise ValidationError(_("Informe as versões e a justificativa da decisão do operador."))
        if policy_version == self.privacy_policy_version or notice_version == self.privacy_notice_version:
            raise ValidationError(_("Use novas versões para a política e o aviso informativo."))
        self.write({
            "website_tracking_policy": "informational_notice",
            "privacy_legal_basis_code": False,
            "privacy_policy_version": policy_version,
            "privacy_notice_version": notice_version,
            "privacy_policy_justification": justification.strip(),
        })
        return {"endpoint_id": self.id, "binding_id": binding.id,
                "website_id": binding.website_id.id, "company_id": self.company_id.id,
                "config_revision": self.config_revision,
                "website_tracking_policy": self.website_tracking_policy}

    consent_ttl_days = fields.Integer(
        default=999,
        required=True,
        help="Technical validity of an individual consent receipt; "
        "independent of attribution-history retention.",
    )

    @api.constrains("consent_ttl_days")
    def _check_consent_ttl(self):
        if any(not 1 <= endpoint.consent_ttl_days <= 3650 for endpoint in self):
            raise ValidationError(
                _("Consent receipt validity must be between 1 and 3650 days.")
            )

    @api.model
    def _privacy_policy_fields(self):
        return super()._privacy_policy_fields() | {"consent_ttl_days", "website_tracking_policy"}

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
        if self.privacy_legal_basis_code == "consent" or self._informational_notice():
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
        if allowed and self._requires_individual_consent():
            return bool(self.env["marketing.website.consent"]._current(self))
        return allowed

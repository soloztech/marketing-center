"""Opt-in routing from an existing Website action to a Contact Center inbox."""

import re
from urllib.parse import urlsplit

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.http import request

from odoo.addons.marketing_center_web_ingress.services.contracts import (
    WebIngressContractError, normalize_allowed_hosts, normalize_allowed_origins,
    normalize_origin,
)
from odoo.addons.marketing_center_website.services.contracts import (
    WebsiteActionContractError, safe_relative_path,
)
from odoo.addons.marketing_center_website_crm.models.native_submission import safe_page


def account_whatsapp_digits(account):
    """Normalize only an account's explicit phone identity, never a remote LID."""
    if not account or (account.platform or "").lower() != "whatsapp":
        return ""
    value = (account.own_external_identity or "").strip()
    if "@" in value:
        value, domain = value.rsplit("@", 1)
        if domain.lower() not in {"s.whatsapp.net", "c.us"}:
            return ""
        value = value.split(":", 1)[0]
    value = re.sub(r"[ +().-]", "", value)
    return value if re.fullmatch(r"[1-9][0-9]{7,14}", value) else ""


class MarketingWebsiteAction(models.Model):
    _inherit = "marketing.website.action"

    handoff_enabled = fields.Boolean(
        string="Vincular conversa do WhatsApp", default=False,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    handoff_account_id = fields.Many2one(
        "contact.center.account", string="Caixa do Contact Center",
        ondelete="restrict", check_company=True,
        domain="[('company_id', '=', company_id), ('platform', '=', 'whatsapp')]",
        groups="marketing_center_base.group_marketing_center_admin",
    )
    handoff_reference_prefix = fields.Char(
        string="Prefixo da referência", default="SITE", size=8,
        groups="marketing_center_base.group_marketing_center_admin",
        help="De 2 a 8 letras maiúsculas ou números. Exemplo: SITE ou CP.",
    )

    def _handoff_account_matches(self):
        self.ensure_one()
        account = self.handoff_account_id
        return bool(
            self.kind == "whatsapp_handoff"
            and account and account.active
            and account.company_id == self.company_id
            and account_whatsapp_digits(account) == self.whatsapp_destination
        )

    @api.constrains(
        "handoff_enabled", "handoff_account_id", "handoff_reference_prefix",
        "kind", "whatsapp_destination", "company_id",
    )
    def _check_handoff_configuration(self):
        for action in self:
            if not re.fullmatch(r"[A-Z0-9]{2,8}", action.handoff_reference_prefix or ""):
                raise ValidationError(_("Use de 2 a 8 letras maiúsculas ou números no prefixo."))
            if action.handoff_enabled and not action._handoff_account_matches():
                raise ValidationError(_(
                    "A vinculação exige uma ação WhatsApp e uma caixa ativa da mesma "
                    "empresa, com o número de destino cadastrado como identidade própria."
                ))

    def write(self, values):
        if {"handoff_enabled", "handoff_account_id", "handoff_reference_prefix"}.intersection(values):
            self.check_access_rights("write")
            self.check_access_rule("write")
            self.mapped("binding_id")._fence_effective_configuration()
        return super().write(values)


class Website(models.Model):
    _inherit = "website"

    handoff_default_action_id = fields.Many2one(
        "marketing.website.action", string="Regra padrão do WhatsApp",
        ondelete="restrict", check_company=True,
        domain="[('website_id', '=', id), ('kind', '=', 'whatsapp_handoff'), "
               "('active', '=', True), ('handoff_enabled', '=', True)]",
        groups="marketing_center_base.group_marketing_center_admin",
        help="Reutilizada nas páginas públicas sem regra própria. Páginas com uma "
             "regra própria mantêm seu número e prefixo, inclusive quando desativadas.",
    )

    @api.constrains("handoff_default_action_id", "company_id")
    def _check_handoff_default_action(self):
        for website in self:
            action = website.handoff_default_action_id
            binding = website._marketing_measurement_binding()
            if action and not website._handoff_action_available(action, binding):
                raise ValidationError(_(
                    "Selecione uma ação WhatsApp ativa deste site e empresa, com "
                    "captura nativa habilitada e caixa de destino válida."
                ))

    def write(self, values):
        if "handoff_default_action_id" in values:
            self.check_access_rights("write")
            self.check_access_rule("write")
            for website in self:
                binding = website._marketing_measurement_binding(active=False)
                if binding:
                    binding._fence_effective_configuration()
        return super().write(values)

    def _handoff_action_available(self, action, binding):
        self.ensure_one()
        return bool(
            binding and binding.active and binding.capture_mode == "native"
            and binding.endpoint_id.active
            and binding.website_id == self and binding.company_id == self.company_id
            and binding.endpoint_id.company_id == self.company_id
            and action and action.active and action.binding_id == binding
            and action.website_id == self and action.company_id == self.company_id
            and action.kind == "whatsapp_handoff" and action.handoff_enabled
            and action._handoff_account_matches()
        )

    def _whatsapp_handoff_action(self, path):
        """One effective rule for rendering and POST; disabled overrides stay off."""
        self.ensure_one()
        website = self.sudo()
        actions = self.env["marketing.website.action"].sudo()
        try:
            path = safe_relative_path(path, "path")
        except WebsiteActionContractError:
            return actions.browse()
        # Reuse the native acquisition URL boundary without needing a request or
        # changing any Website/GA/form tracking eligibility.
        if not safe_page("https://handoff.invalid" + path, "https://handoff.invalid"):
            return actions.browse()
        pages = self.env["website.page"].sudo()
        page = pages.search([
            ("url", "=", path), ("website_id", "=", website.id),
        ], limit=1)
        if not page:
            page = pages.search([("url", "=", path), ("website_id", "=", False)], limit=1)
        # Select the Website override before checking visibility. A private or
        # unpublished override must not fall through to a shared public page.
        if not page or not page.is_published or page.visibility not in (False, ""):
            return actions.browse()
        binding = website._marketing_measurement_binding()
        if not binding or binding.capture_mode != "native":
            return actions.browse()
        specific = actions.search([
            ("binding_id", "=", binding.id), ("active", "=", True),
            ("kind", "=", "whatsapp_handoff"), ("source_path", "=", path),
        ], limit=2)
        if len(specific) > 1:
            return actions.browse()
        # An explicit active page rule with linking switched off is a deliberate
        # exception. Only absence of a page rule may use the Website default.
        candidate = specific or website.handoff_default_action_id
        return candidate if website._handoff_action_available(candidate, binding) else actions.browse()

    def _whatsapp_handoff_config(self):
        """Own public configuration: thank-you pages do not enable GA or forms."""
        self.ensure_one()
        if (not request or not request.env.user._is_public()
                or request.httprequest.scheme != "https" or request.website != self):
            return {}
        path = request.httprequest.path
        action = self._whatsapp_handoff_action(path)
        if not action:
            return {}
        endpoint = action.binding_id.endpoint_id
        try:
            origin = normalize_origin(request.httprequest.host_url)
            if (origin not in normalize_allowed_origins(endpoint.allowed_origins)
                    or urlsplit(origin).hostname not in normalize_allowed_hosts(endpoint.allowed_hosts)):
                return {}
        except WebIngressContractError:
            return {}
        # The destination is already public in the page's links. Editorial text
        # stays in the CMS; the capture request accepts only action and event IDs.
        return {"action": action.public_ref, "destination": action.whatsapp_destination,
                "path": path, "website_id": self.id}


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    handoff_default_action_id = fields.Many2one(
        related="website_id.handoff_default_action_id", readonly=False,
        domain="[('website_id', '=', website_id), ('kind', '=', 'whatsapp_handoff'), "
               "('active', '=', True), ('handoff_enabled', '=', True)]",
    )

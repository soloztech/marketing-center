"""Opt-in routing from an existing Website action to a Contact Center inbox."""

import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


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

    def _marketing_measurement_config(self):
        result = super()._marketing_measurement_config()
        if result.get("capture_mode") == "native" and result.get("whatsapp"):
            action = self.env["marketing.website.action"].sudo().search([
                ("public_ref", "=", result["whatsapp"]),
                ("website_id", "=", self.id),
                ("company_id", "=", self.company_id.id),
            ], limit=1)
            enabled = bool(
                action and action.handoff_enabled and action._handoff_account_matches()
            )
            result = dict(
                result, whatsapp_handoff_enabled=enabled,
                # This business number is already public in the page's links.
                # Editorial messages remain in the CMS; only routing is exposed.
                whatsapp_handoff_destination=action.whatsapp_destination if enabled else "",
            )
        return result

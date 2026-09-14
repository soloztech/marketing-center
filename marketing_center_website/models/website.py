import hashlib
import re
from urllib.parse import urlsplit

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.http import request

from odoo.addons.marketing_center_web_ingress.services.contracts import (
    WebIngressContractError,
    normalize_allowed_hosts,
    normalize_allowed_origins,
    normalize_origin,
)


class Website(models.Model):
    _inherit = "website"

    marketing_cookie_notice_text = fields.Text(
        string="Texto do aviso de cookies",
        translate=True,
        default="Usamos cookies e tecnologias semelhantes para melhorar a experiencia do site. "
        "Ao continuar navegando, você está ciente desse uso.",
    )
    marketing_cookie_proceed_label = fields.Char(
        string="Texto do botão de continuar", translate=True, default="Prosseguir",
    )
    marketing_cookie_policy_url = fields.Char(
        string="Página de privacidade", default="/cookie-policy",
    )

    @api.constrains(
        "marketing_cookie_notice_text",
        "marketing_cookie_proceed_label", "marketing_cookie_policy_url",
    )
    def _check_marketing_cookie_notice(self):
        for website in self:
            for name, limit in (
                ("marketing_cookie_notice_text", 4000),
                ("marketing_cookie_proceed_label", 80),
            ):
                value = website[name] or ""
                if not value.strip() or len(value) > limit:
                    raise ValidationError(_("Preencha o texto do aviso e do botão dentro do limite permitido."))
            value = website.marketing_cookie_policy_url or ""
            try:
                parsed = urlsplit(value)
            except ValueError as error:
                raise ValidationError(_("O endereço da página de privacidade é inválido.")) from error
            if (
                not value or len(value) > 2048 or "\\" in value
                or any(ord(char) < 33 for char in value)
                or parsed.username or parsed.password
                or not (
                    (value.startswith("/") and not value.startswith("//") and not parsed.netloc)
                    or (parsed.scheme == "https" and parsed.hostname)
                )
            ):
                raise ValidationError(_("Use um caminho do site ou um endereço HTTPS para a página de privacidade."))

    def _marketing_measurement_binding(self, active=True):
        self.ensure_one()
        domain = [("website_id", "=", self.id), ("company_id", "=", self.company_id.id)]
        if active:
            domain += [("active", "=", True), ("endpoint_id.active", "=", True)]
        return self.env["marketing.website.ingress.binding"].sudo().with_context(
            active_test=False,
        ).search(domain, limit=1)

    def _marketing_informational_notice(self):
        """The notice remains informational when capture is paused.

        This server-rendered flag precedes Odoo's popup startup, so a delayed
        authorization request cannot turn the notice into native choice buttons.
        """
        binding = self._marketing_measurement_binding(active=False)
        return bool(binding and binding.endpoint_id._informational_notice())

    def _marketing_notice_storage_key(self):
        self.ensure_one()
        text = "\n".join((self.marketing_cookie_notice_text or "",
                          self.marketing_cookie_proceed_label or "",
                          self.marketing_cookie_policy_url or ""))
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        return "marketing_center.website.notice.dismissed.%s.%s" % (self.id, digest)

    def _marketing_measurement_managed(self):
        """Do not revive OCB's Google loader when a configured binding is paused."""
        return bool(self._marketing_measurement_binding(active=False))

    def _marketing_measurement_config(self):
        """Request-scoped public configuration; authorization stays in the ingress."""
        self.ensure_one()
        if not request or not request.env.user._is_public():
            return {}
        if request.httprequest.scheme != "https" or request.website.id != self.id:
            return {}
        binding = self._marketing_measurement_binding()
        if not binding:
            return {}
        endpoint = binding.endpoint_id
        origin = request.httprequest.host_url
        try:
            if (
                normalize_origin(origin) not in normalize_allowed_origins(endpoint.allowed_origins)
                or urlsplit(origin).hostname not in normalize_allowed_hosts(endpoint.allowed_hosts)
            ):
                return {}
        except WebIngressContractError:
            return {}
        path = request.httprequest.path
        if path == "/contactus-thank-you" or path.startswith(("/web/", "/my/", "/website/", "/marketing/")):
            return {}
        page = self.env["website.page"].sudo().search([
            ("url", "=", path), ("website_id", "in", [False, self.id]),
            ("is_published", "=", True), ("visibility", "in", [False, ""]),
        ], limit=1)
        if not page:
            return {}
        result = {
            "form": "", "whatsapp": "", "whatsapp_link_hash": "", "ga4": "",
            "path": path, "website_id": self.id,
            "cookie_notice": self.marketing_cookie_notice_text or "",
            "cookie_proceed_label": self.marketing_cookie_proceed_label or "",
            "cookie_policy_url": self.marketing_cookie_policy_url or "/cookie-policy",
        }
        actions = self.env["marketing.website.action"].sudo().search([
            ("binding_id", "=", binding.id), ("active", "=", True), ("source_path", "=", path),
        ])
        forms = actions.filtered(lambda action: action.kind == "form_submission" and action.form_model_name == "crm.lead")
        if len(forms) == 1:
            result["form"] = forms.public_ref
        whatsapp = actions.filtered(lambda action: action.kind == "whatsapp_handoff")
        if len(whatsapp) == 1:
            result["whatsapp"] = whatsapp.public_ref
            # Compare an existing public CTA without advertising an administrator-only
            # destination/message or allowing the browser to change a signed action.
            canonical = whatsapp.whatsapp_destination + "\n" + (whatsapp.whatsapp_message or "")
            result["whatsapp_link_hash"] = hashlib.sha256(canonical.encode()).hexdigest()
        key = self.google_analytics_key or ""
        if re.fullmatch(r"G-[A-Z0-9]{4,20}", key):
            result["ga4"] = key
        return result

    def write(self, values):
        company_id = values.get("company_id")
        if self and "company_id" in values:
            self.check_access_rights("write")
            self.check_access_rule("write")
            self.env.cr.execute(
                "SELECT id FROM website WHERE id IN %s ORDER BY id FOR UPDATE",
                [tuple(sorted(self.ids))],
            )
            binding = (
                self.env["marketing.website.ingress.binding"]
                .sudo()
                .search(
                    [
                        ("active", "=", True),
                        ("website_id", "in", self.ids),
                        ("endpoint_id.company_id", "!=", company_id),
                    ],
                    limit=1,
                )
            )
            if binding:
                raise ValidationError(
                    _(
                        "Archive or reconfigure the website ingress binding before "
                        "moving the website to another company."
                    )
                )
        return super().write(values)

import hashlib
import re
from urllib.parse import urlsplit

from odoo import _, models
from odoo.exceptions import ValidationError
from odoo.http import request

from ..services.ingress.contracts import (
    WebIngressContractError,
    normalize_allowed_hosts,
    normalize_allowed_origins,
    normalize_origin,
)


class Website(models.Model):
    _inherit = "website"

    def _marketing_measurement_binding(self, active=True):
        self.ensure_one()
        domain = [("website_id", "=", self.id), ("company_id", "=", self.company_id.id)]
        if active:
            domain += [("active", "=", True), ("endpoint_id.active", "=", True)]
        return (
            self.env["marketing.website.ingress.binding"]
            .sudo()
            .with_context(
                active_test=False,
            )
            .search(domain, limit=1)
        )

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
            if normalize_origin(origin) not in normalize_allowed_origins(
                endpoint.allowed_origins
            ) or urlsplit(origin).hostname not in normalize_allowed_hosts(
                endpoint.allowed_hosts
            ):
                return {}
        except WebIngressContractError:
            return {}
        path = request.httprequest.path
        if path == "/contactus-thank-you" or path.startswith(
            ("/web/", "/my/", "/website/", "/marketing/")
        ):
            return {}
        page = (
            self.env["website.page"]
            .sudo()
            .search(
                [
                    ("url", "=", path),
                    ("website_id", "in", [False, self.id]),
                    ("is_published", "=", True),
                    ("visibility", "in", [False, ""]),
                ],
                limit=1,
            )
        )
        if not page:
            return {}
        result = {
            "capture_mode": binding.capture_mode,
            "form": "",
            "whatsapp": "",
            "whatsapp_link_hash": "",
            "ga4": "",
            "path": path,
            "website_id": self.id,
        }
        actions = (
            self.env["marketing.website.action"]
            .sudo()
            .search(
                [
                    ("binding_id", "=", binding.id),
                    ("active", "=", True),
                    ("source_path", "=", path),
                ]
            )
        )
        forms = actions.filtered(
            lambda action: action.kind == "form_submission"
            and action.form_model_name == "crm.lead"
        )
        if len(forms) == 1:
            result["form"] = forms.public_ref
        whatsapp = actions.filtered(lambda action: action.kind == "whatsapp_handoff")
        if len(whatsapp) == 1:
            result["whatsapp"] = whatsapp.public_ref
            # Compare an existing public CTA without advertising an administrator-only
            # destination/message or allowing the browser to change a signed action.
            canonical = (
                whatsapp.whatsapp_destination + "\n" + (whatsapp.whatsapp_message or "")
            )
            result["whatsapp_link_hash"] = hashlib.sha256(
                canonical.encode()
            ).hexdigest()
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

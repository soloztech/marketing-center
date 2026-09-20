"""A small immutable record of a Website click, using the native visitor."""
import datetime
import re
import secrets
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.marketing_center_base.services.serialization import (
    acquire_advisory_xact_lock,
)

HANDOFF_TOKEN = object()
ADMIN = "contact_center_base.group_contact_center_admin"


class WebsiteWhatsappHandoff(models.Model):
    _name = "marketing.website.whatsapp.handoff"
    _description = "Website to WhatsApp click"
    _rec_name = "reference"
    _order = "clicked_at desc, id desc"
    _check_company_auto = True

    reference = fields.Char(string="Referência", required=True, readonly=True, index=True, copy=False)
    action_id = fields.Many2one("marketing.website.action", required=True, readonly=True, ondelete="restrict")
    website_id = fields.Many2one("website", required=True, readonly=True, ondelete="restrict")
    company_id = fields.Many2one("res.company", required=True, readonly=True, index=True, ondelete="restrict")
    account_id = fields.Many2one("contact.center.account", string="WhatsApp de destino", required=True, readonly=True, index=True, ondelete="restrict", check_company=True)
    visitor_id = fields.Many2one("website.visitor", string="Visitante", readonly=True, ondelete="set null", groups=ADMIN)
    track_id = fields.Many2one("website.track", string="Visita de origem", readonly=True, ondelete="set null", groups=ADMIN)
    clicked_at = fields.Datetime(string="Horário do clique", required=True, readonly=True, index=True, default=fields.Datetime.now)
    page_url = fields.Char(string="Página do clique", required=True, readonly=True)
    landing_url = fields.Char(string="Página de aquisição", readonly=True)
    acquisition_json = fields.Json(string="Dados de aquisição", readonly=True, groups=ADMIN)
    event_id = fields.Char(required=True, readonly=True, copy=False, index=True, groups=ADMIN)
    session_key = fields.Char(required=True, readonly=True, copy=False, index=True, groups=ADMIN)
    # Delimited canonical UUIDs preserve retries even when two rapid activations
    # reuse one reference. Bounded at 100 aliases per reference.
    event_refs = fields.Text(readonly=True, copy=False, groups=ADMIN)

    _sql_constraints = [
        ("reference_unique", "unique(reference)", "A referência já existe."),
        ("website_event_unique", "unique(website_id, event_id)", "Este clique já foi registrado."),
    ]

    def _service(self):
        return self.sudo().with_context(website_whatsapp_handoff_token=HANDOFF_TOKEN)

    def _check_service(self):
        if self.env.context.get("website_whatsapp_handoff_token") is not HANDOFF_TOKEN:
            raise AccessError(_("Cliques do site são registrados pelo serviço de captura."))

    @api.model_create_multi
    def create(self, values_list):
        self._check_service()
        return super().create(values_list)

    def write(self, values):
        self._check_service()
        return super().write(values)

    @api.constrains("action_id", "website_id", "company_id", "account_id", "track_id", "visitor_id")
    def _check_scope(self):
        for record in self:
            action = record.action_id
            if (
                action.binding_id.website_id != record.website_id
                or record.website_id.company_id != record.company_id
                or record.account_id.company_id != record.company_id
                or record.account_id.platform != "whatsapp"
                or (record.track_id and record.track_id.visitor_id != record.visitor_id)
            ):
                raise ValidationError(_("O clique, o site e a conta devem pertencer ao mesmo contexto."))

    @api.model
    def _record_click(self, action, visitor, event_id, session_key, page_url,
                      landing_url, acquisition, track=None):
        """Private admission; HTTP performs origin/policy and action validation."""
        action.ensure_one()
        action = action.sudo()
        binding = action.binding_id
        if (
            not action.active or not action.handoff_enabled
            or action.kind != "whatsapp_handoff"
            or not binding.active or binding.capture_mode != "native"
            or not action.handoff_account_id.active
        ):
            raise ValidationError(_("A captura de WhatsApp não está disponível nesta ação."))
        try:
            if str(uuid.UUID(event_id)) != event_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValidationError(_("Identificador de clique inválido.")) from None
        if not isinstance(session_key, str) or not re.fullmatch(r"[a-f0-9]{64}", session_key):
            raise ValidationError(_("Sessão de clique inválida."))
        # Validate the configured number and account on every admission, not only
        # when an administrator saves configuration (the account can change).
        action._check_handoff_configuration()
        service = self._service()
        website = binding.website_id
        acquire_advisory_xact_lock(self.env.cr, "website_whatsapp_event:%s:%s" % (website.id, event_id))
        acquire_advisory_xact_lock(self.env.cr, "website_whatsapp_session:%s:%s" % (action.id, session_key))
        existing = service.search([
            ("website_id", "=", website.id), "|", ("event_id", "=", event_id),
            ("event_refs", "like", "|%s|" % event_id),
        ], limit=1)
        if existing:
            if (existing.action_id != action or existing.session_key != session_key
                    or existing.account_id != action.handoff_account_id
                    or existing.page_url != page_url):
                raise ValidationError(_("Este identificador pertence a outro clique."))
            return existing
        now = fields.Datetime.now()
        recent = service.search([
            ("action_id", "=", action.id), ("session_key", "=", session_key),
            ("account_id", "=", action.handoff_account_id.id),
            ("clicked_at", ">=", now - datetime.timedelta(seconds=60)),
            ("clicked_at", "<=", now), ("page_url", "=", page_url),
        ], limit=1)
        if (recent and recent.acquisition_json == acquisition
                and len(recent.event_refs or "") < 3900):
            recent.write({"event_refs": (recent.event_refs or "") + "|%s|" % event_id})
            return recent
        prefix = action.handoff_reference_prefix or "SITE"
        # 60 random bits; unique database constraint remains the final arbiter.
        alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
        reference = prefix + "-" + "".join(secrets.choice(alphabet) for _ in range(12))
        return service.create({
            "reference": reference, "action_id": action.id, "website_id": website.id,
            "company_id": website.company_id.id, "account_id": action.handoff_account_id.id,
            "visitor_id": visitor.id if visitor else False,
            "track_id": track.id if track else False, "clicked_at": now,
            "page_url": page_url, "landing_url": landing_url,
            "acquisition_json": acquisition, "event_id": event_id,
            "event_refs": "|%s|" % event_id, "session_key": session_key,
        })

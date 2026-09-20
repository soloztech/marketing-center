"""Website handoff associations; temporal evidence never claims an identity."""

import datetime
import re

from psycopg2 import IntegrityError

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.tools import html2plaintext

from odoo.addons.marketing_center_base.services.serialization import (
    MarketingSerializationFailure,
    acquire_advisory_xact_lock,
)

MATCH_TOKEN = object()
CLAIMED_STATES = ("reference", "confirmed")
REFERENCE_RE = re.compile(r"(?<![A-Za-z0-9-])([A-Z0-9]{2,8}-[A-Z0-9]{12})(?![A-Za-z0-9-])")
REFERENCE_TOKEN_HINT_RE = re.compile(
    r"\b[A-Za-z0-9]{2,8}[-‐‑‒–—][A-Za-z0-9]{6,32}\b"
)
REFERENCE_HINT_RE = re.compile(
    r"refer[eê]ncia\s*:|\b[A-Za-z0-9]{2,8}[-‐‑‒–—][A-Za-z0-9]{6,32}\b",
    re.IGNORECASE,
)
REFERENCE_TTL = datetime.timedelta(days=30)
TEMPORAL_WINDOW = datetime.timedelta(minutes=10)
RETURN_GAP = datetime.timedelta(hours=24)
ROUNDING_ALLOWANCE = datetime.timedelta(seconds=2)
CANDIDATE_LIMIT = 5


class WebsiteWhatsappMatch(models.Model):
    _name = "marketing.website.whatsapp.match"
    _description = "Website WhatsApp association"
    _rec_name = "reference"
    _order = "create_date desc, score desc, id desc"
    _check_company_auto = True

    handoff_id = fields.Many2one(
        "marketing.website.whatsapp.handoff", required=True, readonly=True,
        ondelete="cascade", index=True, check_company=True,
    )
    message_binding_id = fields.Many2one(
        "contact.center.message.binding", required=True, readonly=True,
        ondelete="cascade", index=True, check_company=True,
    )
    channel_id = fields.Many2one(
        related="message_binding_id.channel_binding_id.channel_id",
        store=True, readonly=True, index=True,
    )
    account_id = fields.Many2one(
        related="handoff_id.account_id", store=True, readonly=True, index=True,
    )
    company_id = fields.Many2one(
        related="handoff_id.company_id", store=True, readonly=True, index=True,
    )
    state = fields.Selection(
        [("reference", "Vinculado pela referência"),
         ("suggested", "Associação sugerida"),
         ("confirmed", "Confirmado pela equipe"),
         ("rejected", "Descartado / desfeito")],
        required=True, readonly=True, index=True,
    )
    delta_seconds = fields.Integer(string="Intervalo (segundos)", readonly=True)
    score = fields.Integer(
        string="Pontuação de ordenação", readonly=True,
        help="Ordena sugestões pela proximidade temporal; não é uma probabilidade.",
    )
    candidate_count = fields.Integer(string="Candidatos no intervalo", readonly=True)
    candidates_truncated = fields.Boolean(string="Há outros candidatos", readonly=True)
    reason = fields.Char(string="Evidência", required=True, readonly=True)
    reviewer_id = fields.Many2one("res.users", string="Revisado por", readonly=True)
    reviewed_at = fields.Datetime(string="Revisado em", readonly=True)
    reference = fields.Char(related="handoff_id.reference", readonly=True)
    page_url = fields.Char(related="handoff_id.page_url", readonly=True)
    landing_url = fields.Char(related="handoff_id.landing_url", readonly=True)
    message_at = fields.Datetime(related="message_binding_id.message_id.date", readonly=True)

    _sql_constraints = [
        ("handoff_message_unique", "unique(handoff_id, message_binding_id)",
         "Esta associação já foi analisada."),
        ("score_bounds", "check(score >= 0 and score <= 100)",
         "A pontuação deve estar entre 0 e 100."),
    ]

    def init(self):
        # One click cannot identify two chats; one message cannot claim two clicks.
        for field in ("handoff_id", "message_binding_id"):
            self.env.cr.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS marketing_whatsapp_claim_%s "
                "ON marketing_website_whatsapp_match (%s) "
                "WHERE state IN ('reference', 'confirmed')" % (field, field)
            )

    def _service(self):
        return self.sudo().with_context(marketing_whatsapp_match_service=MATCH_TOKEN)

    def _check_service(self):
        if self.env.context.get("marketing_whatsapp_match_service") is not MATCH_TOKEN:
            raise AccessError(_("Use a revisão de vínculos de WhatsApp."))

    @api.model_create_multi
    def create(self, vals_list):
        self._check_service()
        return super().create(vals_list)

    def write(self, values):
        self._check_service()
        if set(values) - {"state", "reviewer_id", "reviewed_at"}:
            raise AccessError(_("A evidência original da associação é imutável."))
        return super().write(values)

    def unlink(self):
        self._check_service()
        return super().unlink()

    @api.constrains("handoff_id", "message_binding_id", "state")
    def _check_scope(self):
        for match in self:
            message = match.message_binding_id
            if (
                match.company_id != message.company_id
                or match.account_id != message.account_id
                or message.direction != "inbound"
                or message.origin != "provider"
                or message.channel_binding_id.conversation_type != "direct"
                or message.account_id.platform != "whatsapp"
            ):
                raise ValidationError(_("A mensagem e o clique devem ter a mesma caixa e empresa."))

    def _authorized_review(self):
        self.ensure_one()
        self.check_access_rights("read")
        self.check_access_rule("read")
        self.check_access_rights("write")
        self.check_access_rule("write")
        if self.company_id not in self.env.companies:
            raise AccessError(_("A empresa da associação não está ativa."))
        channel, _member = self.env["contact.center.ui.api"]._authorized_channel(self.channel_id.id)
        channel.check_access_rights("write")
        channel.check_access_rule("write")
        self.account_id._contact_center_check_user_scope()
        return True

    def action_confirm(self):
        self._authorized_review()
        service = self.env["marketing.website.whatsapp.correlation"]
        service._lock_account(self.account_id)
        self.invalidate_recordset()
        self._authorized_review()
        if self.state in CLAIMED_STATES:
            return True
        if self.state != "suggested":
            raise ValidationError(_("Apenas sugestões pendentes podem ser confirmadas."))
        competitors = self._service().search([
            ("state", "in", CLAIMED_STATES),
            "|", ("handoff_id", "=", self.handoff_id.id),
            ("message_binding_id", "=", self.message_binding_id.id),
        ], limit=1)
        if competitors:
            raise ValidationError(_("Este clique ou mensagem já tem outro vínculo confirmado."))
        try:
            with self.env.cr.savepoint():
                self._service().write({
                    "state": "confirmed", "reviewer_id": self.env.uid,
                    "reviewed_at": fields.Datetime.now(),
                })
        except IntegrityError as error:
            if error.pgcode == "23505":
                raise MarketingSerializationFailure(
                    "Concurrent WhatsApp confirmation requires a fresh snapshot"
                ) from error
            raise
        return True

    def action_reject(self):
        self._authorized_review()
        self.env["marketing.website.whatsapp.correlation"]._lock_account(self.account_id)
        self.invalidate_recordset()
        self._authorized_review()
        if self.state == "rejected":
            return True
        self._service().write({
            "state": "rejected", "reviewer_id": self.env.uid,
            "reviewed_at": fields.Datetime.now(),
        })
        return True


class WebsiteWhatsappHandoff(models.Model):
    _inherit = "marketing.website.whatsapp.handoff"

    match_ids = fields.One2many("marketing.website.whatsapp.match", "handoff_id", readonly=True)


class WebsiteWhatsappCorrelation(models.AbstractModel):
    _name = "marketing.website.whatsapp.correlation"
    _description = "Website WhatsApp association service"

    @api.model
    def _lock_account(self, account):
        acquire_advisory_xact_lock(
            self.env.cr, "marketing_website_whatsapp:account:%s" % account.id,
        )

    @api.model
    def _eligible(self, message):
        return bool(
            message.exists()
            and message.direction == "inbound"
            and message.origin == "provider"
            and message.message_state == "active"
            and not message.is_forwarded
            and not message.reply_to_binding_id
            and message.channel_binding_id.conversation_type == "direct"
            and message.account_id.platform == "whatsapp"
            and message.company_id == message.account_id.company_id
            and message.company_id in self.env.companies
            and message.message_id.date
        )

    @api.model
    def _is_entry_message(self, message):
        when = message.message_id.date
        previous = self.env["contact.center.message.binding"].sudo().search([
            ("channel_binding_id", "=", message.channel_binding_id.id),
            ("id", "!=", message.id),
            "|", ("message_id.date", "<", when),
            "&", ("message_id.date", "=", when), ("id", "<", message.id),
            ("message_id.date", ">", when - RETURN_GAP),
        ], limit=1)
        return not previous

    @api.model
    def _create_association(self, handoff, message, state, count=1, score=0, truncated=False):
        matches = self.env["marketing.website.whatsapp.match"]._service()
        existing = matches.search([
            ("handoff_id", "=", handoff.id),
            ("message_binding_id", "=", message.id),
        ], limit=1)
        if existing:
            return existing
        delta = int((message.message_id.date - handoff.clicked_at).total_seconds())
        reason = (
            _("Referência exata recebida nesta caixa; intervalo de %s segundos.") % delta
            if state == "reference" else
            _("Proximidade temporal: %s segundos; %s candidato(s) na mesma caixa. "
              "Sugestão sem confirmação de identidade.") % (delta, count)
        )
        if truncated:
            reason += _(" Há pelo menos %s candidatos; exibindo somente os %s mais próximos.") % (count, CANDIDATE_LIMIT)
        try:
            with self.env.cr.savepoint():
                return matches.create({
                    "handoff_id": handoff.id, "message_binding_id": message.id,
                    "state": state, "delta_seconds": delta, "score": score,
                    "candidate_count": count, "reason": reason,
                    "candidates_truncated": truncated,
                })
        except IntegrityError as error:
            if error.pgcode == "23505":
                raise MarketingSerializationFailure(
                    "Concurrent WhatsApp association requires a fresh snapshot"
                ) from error
            raise

    @api.model
    def _analyze_inbound(self, message):
        """Inspect only this newly persisted message; no history backfill or I/O."""
        message.ensure_one()
        empty = self.env["marketing.website.whatsapp.match"]
        if not self._eligible(message):
            return empty
        self._lock_account(message.account_id)
        message.invalidate_recordset()
        if not self._eligible(message):
            return empty
        matches = empty._service()
        processed = matches.search([("message_binding_id", "=", message.id)])
        if processed:
            return processed
        text = html2plaintext(message.message_id.body or "")
        if len(text) > 8192:
            return empty
        references = REFERENCE_RE.findall(text)
        when = message.message_id.date
        base_domain = [
            ("account_id", "=", message.account_id.id),
            ("company_id", "=", message.company_id.id),
            ("clicked_at", "<=", when + ROUNDING_ALLOWANCE),
        ]
        handoffs = self.env["marketing.website.whatsapp.handoff"].sudo()
        if references:
            if len(references) != 1 or REFERENCE_TOKEN_HINT_RE.findall(text) != references:
                return empty
            handoff = handoffs.search(base_domain + [
                ("reference", "=", references[0]),
                ("clicked_at", ">=", when - REFERENCE_TTL),
            ], limit=1)
            if not handoff:
                return empty
            claimed = matches.search([
                ("handoff_id", "=", handoff.id), ("state", "in", CLAIMED_STATES),
            ], limit=1)
            if claimed:
                return claimed if claimed.channel_id == message.channel_binding_id.channel_id else empty
            return self._create_association(handoff, message, "reference")
        if REFERENCE_HINT_RE.search(text) or not self._is_entry_message(message):
            return empty
        # In Odoo 16, negating a dotted One2many condition selects parents that
        # HAVE a non-matching child; it excludes clicks with no association at
        # all. Negate the direct relation against the claimed-record subquery.
        # _search returns a Query, so the account's historical IDs are never
        # materialized into an unbounded Python list.
        claimed_query = matches._search([
            ("account_id", "=", message.account_id.id),
            ("company_id", "=", message.company_id.id),
            ("state", "in", CLAIMED_STATES),
        ])
        candidates = handoffs.search(base_domain + [
            ("clicked_at", ">=", when - TEMPORAL_WINDOW),
            ("match_ids", "not in", claimed_query),
        ], order="clicked_at desc, id desc", limit=CANDIDATE_LIMIT + 1)
        truncated = len(candidates) > CANDIDATE_LIMIT
        result = empty
        for handoff in candidates[:CANDIDATE_LIMIT]:
            delta = max(0, (when - handoff.clicked_at).total_seconds())
            score = max(0, 100 - int(delta / TEMPORAL_WINDOW.total_seconds() * 100))
            result |= self._create_association(
                handoff, message, "suggested", len(candidates), score, truncated,
            )
        return result


class MailChannel(models.Model):
    _inherit = "mail.channel"

    marketing_whatsapp_match_ids = fields.One2many(
        "marketing.website.whatsapp.match", "channel_id", readonly=True,
    )


class CrmLead(models.Model):
    _inherit = "crm.lead"

    marketing_whatsapp_match_ids = fields.Many2many(
        "marketing.website.whatsapp.match", compute="_compute_marketing_whatsapp_matches",
        compute_sudo=False, string="Origens do site por WhatsApp",
    )

    @api.depends_context("uid", "allowed_company_ids")
    def _compute_marketing_whatsapp_matches(self):
        matches = self.env["marketing.website.whatsapp.match"]
        for lead in self:
            if not matches.check_access_rights("read", raise_exception=False):
                lead.marketing_whatsapp_match_ids = matches
                continue
            channels = lead._visible_contact_center_channels()
            lead.marketing_whatsapp_match_ids = matches.search([
                ("channel_id", "in", channels.ids),
                ("state", "in", CLAIMED_STATES),
            ]) if channels else matches

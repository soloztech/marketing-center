import datetime
import hashlib
import re
import secrets

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.contracts import WebsiteActionContractError, parse_redirect_token
from ..services.tokens import WEBSITE_ACTION_INTERNAL_TOKEN

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GRANT_RETENTION = datetime.timedelta(hours=1)
_GC_BATCH = 50_000


def _hash(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _utc(value):
    value = value or fields.Datetime.now()
    if not isinstance(value, datetime.datetime):
        raise ValidationError(_("The redirect grant timestamp is invalid."))
    if value.tzinfo:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)


class MarketingWebsiteRedirectGrant(models.Model):
    _name = "marketing.website.redirect.grant"
    _description = "Ephemeral Marketing Website Redirect Grant"
    _order = "expires_at desc, id desc"
    _check_company_auto = True

    action_id = fields.Many2one(
        "marketing.website.action",
        required=True,
        readonly=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    company_id = fields.Many2one(
        related="action_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    token_hash = fields.Char(required=True, readonly=True, size=64, index=True)
    event_key_hash = fields.Char(required=True, readonly=True, size=64, index=True)
    state = fields.Selection(
        [("issued", "Issued"), ("consumed", "Consumed"), ("revoked", "Revoked")],
        required=True,
        default="issued",
        readonly=True,
        index=True,
    )
    expires_at = fields.Datetime(required=True, readonly=True, index=True)
    consumed_at = fields.Datetime(readonly=True)

    _sql_constraints = [
        (
            "token_hash_unique",
            "unique(token_hash)",
            "A redirect grant token digest must be unique.",
        ),
        (
            "token_hash_size",
            "check(length(token_hash) = 64 and length(event_key_hash) = 64)",
            "Redirect grant digests must be SHA-256 values.",
        ),
        (
            "lifecycle_consistent",
            "check((state = 'consumed' and consumed_at is not null) or "
            "(state in ('issued', 'revoked') and consumed_at is null))",
            "Redirect grant lifecycle state is inconsistent.",
        ),
    ]

    def init(self):
        self.env.cr.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "marketing_website_redirect_grant_issued_uniq "
            "ON marketing_website_redirect_grant (action_id, event_key_hash) "
            "WHERE state = 'issued'"
        )

    @api.model_create_multi
    def create(self, vals_list):
        if not self._internal():
            raise AccessError(_("Redirect grants are managed internally."))
        for values in vals_list:
            if not _SHA256_RE.fullmatch(values.get("token_hash", "")) or not (
                _SHA256_RE.fullmatch(values.get("event_key_hash", ""))
            ):
                raise AccessError(_("Redirect grant digests are invalid."))
            if values.get("state", "issued") != "issued" or values.get("consumed_at"):
                raise AccessError(_("A redirect grant must begin in issued state."))
        return super().create(vals_list)

    def write(self, values):
        if not self._internal():
            raise AccessError(_("Redirect grants are managed internally."))
        revoked = set(values) == {"state"} and values.get("state") == "revoked"
        consumed = (
            set(values) == {"state", "consumed_at"}
            and values.get("state") == "consumed"
            and bool(values.get("consumed_at"))
        )
        if not (revoked or consumed) or any(grant.state != "issued" for grant in self):
            raise AccessError(_("The redirect grant transition is invalid."))
        return super().write(values)

    def unlink(self):
        if not self._internal():
            raise AccessError(_("Redirect grants are managed internally."))
        return super().unlink()

    @api.model
    def _internal(self):
        return (
            self.env.context.get("marketing_website_action_internal")
            is WEBSITE_ACTION_INTERNAL_TOKEN
        )

    @api.model
    def _issue(self, action, event_id, now=None):
        action.ensure_one()
        now = _utc(now)
        event_key_hash = _hash(event_id)
        endpoint = action.binding_id.endpoint_id
        self.env.cr.execute(
            "SELECT id FROM marketing_web_ingress_endpoint " "WHERE id = %s FOR SHARE",
            [endpoint.id],
        )
        # A real MVCC row version, rather than a lock-only fence, forces a
        # concurrent issuer that started from an older REPEATABLE READ snapshot
        # through Odoo's serialization retry.  It can then observe and revoke
        # the previously issued grant before creating the replacement.
        self.env.cr.execute(
            "UPDATE marketing_website_action SET write_date = write_date "
            "WHERE id = %s RETURNING id",
            [action.id],
        )
        if not self.env.cr.fetchone():
            raise AccessError(_("The WhatsApp handoff is unavailable."))
        action.invalidate_recordset(
            ["active", "binding_id", "token_ttl_seconds", "kind"]
        )
        binding = action.binding_id
        binding.invalidate_recordset(["active", "endpoint_id"])
        endpoint.invalidate_recordset(["active"])
        if not (
            action.active
            and action.kind == "whatsapp_handoff"
            and binding.active
            and binding.endpoint_id == endpoint
            and endpoint.active
        ):
            raise AccessError(_("The WhatsApp handoff is unavailable."))
        existing = self.sudo().search(
            [
                ("action_id", "=", action.id),
                ("event_key_hash", "=", event_key_hash),
                ("state", "=", "issued"),
            ]
        )
        if existing:
            self.env.cr.execute(
                "SELECT id FROM marketing_website_redirect_grant "
                "WHERE id IN %s ORDER BY id FOR UPDATE",
                [tuple(existing.ids)],
            )
            existing.with_context(
                marketing_website_action_internal=WEBSITE_ACTION_INTERNAL_TOKEN
            ).write({"state": "revoked"})
            # Odoo 16 may defer the physical UPDATE until the next flush.  The
            # partial unique index still sees the old issued row otherwise,
            # so make the lifecycle transition visible before its replacement
            # is inserted in this same transaction.
            existing.flush_recordset(["state"])
        raw_token = secrets.token_urlsafe(32)
        self.sudo().with_company(action.company_id).with_context(
            marketing_website_action_internal=WEBSITE_ACTION_INTERNAL_TOKEN
        ).create(
            {
                "action_id": action.id,
                "token_hash": _hash(raw_token),
                "event_key_hash": event_key_hash,
                "expires_at": now
                + datetime.timedelta(seconds=action.token_ttl_seconds),
            }
        )
        return raw_token

    @api.model
    def _consume(self, raw_token, website, now=None):
        try:
            raw_token = parse_redirect_token(raw_token)
        except WebsiteActionContractError:
            return self.env["marketing.website.action"], "/"
        token_hash = _hash(raw_token)
        # Resolve only technical parent ids, then acquire locks in the same
        # endpoint -> action -> grant order used by configuration and issuance.
        company = website.company_id
        self.env.cr.execute(
            "SELECT redirect_grant.action_id, binding.endpoint_id "
            "FROM marketing_website_redirect_grant AS redirect_grant "
            "JOIN marketing_website_action AS action "
            "ON action.id = redirect_grant.action_id "
            "JOIN marketing_website_ingress_binding AS binding "
            "ON binding.id = action.binding_id "
            "JOIN website AS site ON site.id = binding.website_id "
            "WHERE redirect_grant.token_hash = %s "
            # The binding owns the direct website foreign key.  Do not filter
            # on the stored related fields of ``action``/``binding`` here:
            # their recomputation may still be pending when an action is
            # issued and consumed in the same transaction.
            "AND binding.website_id = %s " "AND site.company_id = %s",
            [token_hash, website.id, company.id],
        )
        row = self.env.cr.fetchone()
        if not row:
            return self.env["marketing.website.action"], "/"
        action_id, endpoint_id = row
        self.env.cr.execute(
            "SELECT id FROM marketing_web_ingress_endpoint " "WHERE id = %s FOR SHARE",
            [endpoint_id],
        )
        if not self.env.cr.fetchone():
            return self.env["marketing.website.action"], "/"
        self.env.cr.execute(
            "SELECT id FROM marketing_website_action WHERE id = %s FOR UPDATE",
            [action_id],
        )
        if not self.env.cr.fetchone():
            return self.env["marketing.website.action"], "/"
        self.env.cr.execute(
            "SELECT id FROM marketing_website_redirect_grant "
            "WHERE token_hash = %s AND action_id = %s FOR UPDATE",
            [token_hash, action_id],
        )
        row = self.env.cr.fetchone()
        if not row:
            return self.env["marketing.website.action"], "/"
        grant = self.sudo().browse(row[0])
        grant.invalidate_recordset(["action_id", "state", "expires_at", "consumed_at"])
        action = grant.action_id
        action.invalidate_recordset(
            ["active", "kind", "website_id", "binding_id", "fallback_path"]
        )
        binding = action.binding_id
        binding.invalidate_recordset(["active", "endpoint_id"])
        endpoint = self.env["marketing.web.ingress.endpoint"].browse(endpoint_id)
        endpoint.invalidate_recordset(["active"])
        fallback = (
            action.fallback_path if action.website_id == website else "/"
        ) or "/"
        now = _utc(now)
        valid = (
            grant.state == "issued"
            and grant.expires_at >= now
            and action.website_id == website
            and action.kind == "whatsapp_handoff"
            and action.active
            and binding.active
            and binding.endpoint_id == endpoint
            and endpoint.active
        )
        internal = grant.with_context(
            marketing_website_action_internal=WEBSITE_ACTION_INTERNAL_TOKEN
        )
        if not valid:
            if grant.state == "issued":
                internal.write({"state": "revoked"})
            return self.env["marketing.website.action"], fallback
        internal.write(
            {
                "state": "consumed",
                "consumed_at": now,
            }
        )
        return action, fallback

    @api.autovacuum
    def _gc_expired_grants(self):
        cutoff = fields.Datetime.now() - _GRANT_RETENTION
        expired = self.sudo().search(
            [
                "|",
                ("expires_at", "<", cutoff),
                "&",
                ("state", "!=", "issued"),
                ("consumed_at", "<", cutoff),
            ],
            limit=_GC_BATCH,
        )
        if expired:
            expired.with_context(
                marketing_website_action_internal=WEBSITE_ACTION_INTERNAL_TOKEN
            ).unlink()

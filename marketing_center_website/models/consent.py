"""Individual Website decisions. A routing key or browser boolean is not consent."""

import datetime
import json
import re
import uuid
from urllib.parse import unquote, urlsplit

from odoo import _, api, fields, models
from odoo.exceptions import AccessError
from odoo.http import request
from odoo.tools.misc import consteq, hmac

CONSENT_CONTEXT_TOKEN = object()
CONSENT_COOKIE = "mc_website_consent"
_SCOPE = "marketing_center_website.individual_consent.v1"
_TOKEN = re.compile(r"^([0-9a-f-]{36})\.([0-9a-f]{64})$")


class MarketingWebsiteConsent(models.Model):
    _name = "marketing.website.consent"
    _description = "Individual Website Attribution Decision"
    _order = "decided_at desc, id desc"
    _rec_name = "public_ref"

    public_ref = fields.Char(readonly=True, index=True, size=36)
    website_id = fields.Many2one(
        "website", required=True, readonly=True, ondelete="restrict"
    )
    endpoint_id = fields.Many2one(
        "marketing.web.ingress.endpoint",
        required=True,
        readonly=True,
        ondelete="restrict",
    )
    company_id = fields.Many2one(
        "res.company", required=True, readonly=True, ondelete="restrict"
    )
    config_revision = fields.Integer(required=True, readonly=True)
    policy_version = fields.Char(required=True, readonly=True, size=128)
    notice_version = fields.Char(required=True, readonly=True, size=128)
    granted = fields.Boolean(readonly=True)
    decided_at = fields.Datetime(required=True, readonly=True)
    expires_at = fields.Datetime(required=True, readonly=True, index=True)
    revoked_at = fields.Datetime(readonly=True, index=True)
    identifier_erased_at = fields.Datetime(readonly=True, index=True)

    _sql_constraints = [
        ("public_ref_unique", "unique(public_ref)", "Consent reference must be unique.")
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("website_consent_internal")
            is not CONSENT_CONTEXT_TOKEN
        ):
            raise AccessError(
                _("Website decisions are managed by their dedicated service.")
            )
        return super().create(vals_list)

    def write(self, values):
        if self.env.context.get(
            "website_consent_internal"
        ) is not CONSENT_CONTEXT_TOKEN or set(values) != {"revoked_at"}:
            raise AccessError(
                _("An individual decision is immutable except for revocation.")
            )
        if not values["revoked_at"] or any(self.mapped("revoked_at")):
            raise AccessError(_("A revoked decision cannot be restored."))
        return super().write(values)

    def unlink(self):
        raise AccessError(_("Individual decision evidence cannot be deleted manually."))

    def _cookie(self):
        self.ensure_one()
        return "%s.%s" % (self.public_ref, hmac(self.env, _SCOPE, self._message()))

    def _message(self):
        self.ensure_one()
        return "|".join(
            (
                self.public_ref,
                str(self.website_id.id),
                str(self.endpoint_id.id),
                str(self.company_id.id),
                str(self.config_revision),
                self.policy_version,
                self.notice_version,
                str(self.decided_at),
                str(self.expires_at),
            )
        )

    @api.model
    def _from_cookie(self, endpoint, raw_cookie):
        match = _TOKEN.fullmatch(raw_cookie or "")
        if not match:
            return self.browse()
        decision = self.sudo().search(
            [
                ("public_ref", "=", match[1]),
                ("endpoint_id", "=", endpoint.id),
                ("company_id", "=", endpoint.company_id.id),
            ],
            limit=1,
        )
        if not decision or not consteq(
            match[2], hmac(decision.env, _SCOPE, decision._message())
        ):
            return self.browse()
        return decision

    def _is_current(self, endpoint, *, require_grant=True):
        if (
            len(self) != 1
            or self.endpoint_id != endpoint
            or self.company_id != endpoint.company_id
            or not self.website_id.cookies_bar
        ):
            return False
        if (
            not self.env["marketing.website.ingress.binding"]
            .sudo()
            .search_count(
                [
                    ("website_id", "=", self.website_id.id),
                    ("endpoint_id", "=", endpoint.id),
                    ("active", "=", True),
                    ("company_id", "=", endpoint.company_id.id),
                ]
            )
        ):
            return False
        # A concurrent revocation must retry from a fresh transaction snapshot.
        self.env.cr.execute(
            "SELECT id FROM marketing_website_consent WHERE id = %s FOR SHARE",
            [self.id],
        )
        self.invalidate_recordset(["revoked_at", "granted", "expires_at"])
        return bool(
            not self.revoked_at
            and (self.granted or not require_grant)
            and self.expires_at > fields.Datetime.now()
            and self.config_revision == endpoint.config_revision
            and self.policy_version == endpoint.privacy_policy_version
            and self.notice_version == endpoint.privacy_notice_version
        )

    @api.model
    def _current(self, endpoint):
        """Resolve the actual HTTP cookie or an already validated internal intent.

        RPC cannot manufacture the identity-only context token. OCA's worker
        itself runs over anonymous HTTP, so a server-validated durable intent
        uses its stored decision before inspecting the worker request cookies.
        Untrusted HTTP/RPC callers still require their actual browser cookie.
        """
        if self.env.context.get("website_consent_internal") is CONSENT_CONTEXT_TOKEN:
            decision = self.browse(
                self.env.context.get("website_consent_id", 0)
            ).exists()
        elif request and getattr(request, "httprequest", None):
            if request.session.uid:
                return self.browse()
            if request.httprequest.scheme != "https":
                return self.browse()
            try:
                native = json.loads(
                    unquote(
                        request.httprequest.cookies.get("website_cookies_bar", "{}")
                    )
                )
            except (ValueError, TypeError):
                return self.browse()
            if not isinstance(native, dict) or native.get("optional") is not True:
                return self.browse()
            decision = self._from_cookie(
                endpoint, request.httprequest.cookies.get(CONSENT_COOKIE, "")
            )
            if (
                decision
                and request.httprequest.path.startswith(
                    ("/marketing/web-ingress/", "/marketing/website-action/")
                )
                and request.httprequest.method == "POST"
            ):
                if (
                    request.httprequest.headers.get("X-Marketing-Consent-Ref")
                    != decision.public_ref
                ):
                    return self.browse()
            if decision:
                if (
                    urlsplit(decision.website_id.domain or "").netloc
                    != request.httprequest.host
                ):
                    return self.browse()
        else:
            return self.browse()
        return decision if decision._is_current(endpoint) else self.browse()

    @api.model
    def _decide(self, binding, granted, previous_cookie="", now=None):
        endpoint = binding.endpoint_id
        # Same ordering as capture: configuration root, then individual decision.
        self.env.cr.execute(
            "SELECT id FROM marketing_web_ingress_endpoint WHERE id = %s FOR SHARE",
            [endpoint.id],
        )
        endpoint.invalidate_recordset(
            ["config_revision"] + list(endpoint._privacy_policy_fields())
        )
        if (
            not endpoint.capture_enabled
            or not endpoint._privacy_policy_configured()
            or not endpoint._requires_individual_consent()
        ):
            raise AccessError(_("Individual attribution policy is unavailable."))
        now = now or fields.Datetime.now()
        previous = self._from_cookie(endpoint, previous_cookie)
        if previous:
            self.env.cr.execute(
                "SELECT id FROM marketing_website_consent WHERE id = %s FOR UPDATE",
                [previous.id],
            )
            previous.invalidate_recordset(["revoked_at"])
            if previous.revoked_at:
                raise AccessError(_("The previous decision has already been revoked."))
            if not previous.revoked_at:
                previous.with_context(
                    website_consent_internal=CONSENT_CONTEXT_TOKEN
                ).write({"revoked_at": now})
        return (
            self.sudo()
            .with_context(website_consent_internal=CONSENT_CONTEXT_TOKEN)
            .create(
                {
                    "public_ref": str(uuid.uuid4()),
                    "website_id": binding.website_id.id,
                    "endpoint_id": endpoint.id,
                    "company_id": endpoint.company_id.id,
                    "config_revision": endpoint.config_revision,
                    "policy_version": endpoint.privacy_policy_version,
                    "notice_version": endpoint.privacy_notice_version,
                    "granted": granted,
                    "decided_at": now,
                    "expires_at": now
                    + datetime.timedelta(days=endpoint.consent_ttl_days),
                }
            )
        )

    @api.autovacuum
    def _gc_expired_decision_identifiers(self):
        expired = self.sudo().search(
            [("expires_at", "<=", fields.Datetime.now()), ("public_ref", "!=", False)],
            limit=100,
            order="expires_at,id",
        )
        if expired:
            # Retain only a minimal policy audit and any historical intent FK;
            # erase the cookie's external identifier at its explicit deadline.
            super(MarketingWebsiteConsent, expired).write(
                {"public_ref": False, "identifier_erased_at": fields.Datetime.now()}
            )

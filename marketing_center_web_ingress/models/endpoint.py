import datetime
import re
import secrets
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.contracts import (
    WebIngressContractError,
    normalize_allowed_hosts,
    normalize_allowed_origins,
)
from ..services.tokens import WEB_INGRESS_INTERNAL_TOKEN


def _uuid(_recordset):
    return str(uuid.uuid4())


def _public_key(_recordset):
    return secrets.token_urlsafe(32)


class MarketingWebIngressEndpoint(models.Model):
    _name = "marketing.web.ingress.endpoint"
    _description = "Marketing Web Ingress Endpoint"
    _order = "company_id, name, id"
    _check_company_auto = True

    name = fields.Char(required=True, index=True)
    active = fields.Boolean(default=True, index=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
        ondelete="restrict",
    )
    public_ref = fields.Char(
        required=True,
        default=_uuid,
        size=36,
        readonly=True,
        copy=False,
        index=True,
    )
    public_key = fields.Char(
        string="Public ingestion key",
        required=True,
        default=_public_key,
        size=128,
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_admin",
        help=(
            "Browser-visible routing capability. It can be rotated/revoked but is "
            "not a secret and is not strong client authentication."
        ),
    )
    key_revision = fields.Integer(
        required=True,
        default=1,
        readonly=True,
        copy=False,
    )
    config_revision = fields.Integer(
        required=True,
        default=1,
        readonly=True,
        copy=False,
    )
    allowed_origins = fields.Text(
        required=True,
        help="One exact HTTP(S) origin per line. Wildcards are not accepted.",
    )
    allowed_hosts = fields.Text(
        required=True,
        help="One exact landing-page hostname per line. Wildcards are not accepted.",
    )
    replay_window_seconds = fields.Integer(required=True, default=300)
    max_body_bytes = fields.Integer(required=True, default=8192)
    max_field_length = fields.Integer(required=True, default=512)
    rate_limit_per_minute = fields.Integer(
        required=True,
        default=1200,
        help=(
            "Application-level ceiling per request class and Odoo database. "
            "The reverse proxy must still enforce the authoritative edge limit."
        ),
    )
    capture_enabled = fields.Boolean(
        default=False,
        help="Enable optional attribution only under the documented policy below. "
        "This is not evidence of visitor consent.",
    )
    capture_purpose = fields.Char()
    privacy_policy_version = fields.Char()
    privacy_notice_version = fields.Char()
    privacy_legal_basis_code = fields.Char(
        help="Documented non-consent legal basis. Consent-based capture is unavailable "
        "until a trusted individual-decision producer is integrated.",
    )
    privacy_policy_justification = fields.Text()
    identifier_retention_days = fields.Integer(
        default=0,
        help="Explicit policy duration; zero means not configured. No legal duration "
        "is supplied by the application.",
    )
    privacy_policy_set_by = fields.Many2one("res.users", readonly=True)
    privacy_policy_set_at = fields.Datetime(readonly=True)
    legacy_retention_count = fields.Integer(compute="_compute_legacy_retention_count")
    event_ids = fields.One2many(
        "marketing.web.ingress.event", "endpoint_id", readonly=True
    )

    _sql_constraints = [
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The web ingress public reference must be unique.",
        ),
        (
            "public_key_unique",
            "unique(public_key)",
            "The web ingress public key must be unique.",
        ),
        (
            "revisions_positive",
            "check(key_revision > 0 and config_revision > 0)",
            "Web ingress revisions must be positive.",
        ),
        (
            "replay_window_bounded",
            "check(replay_window_seconds between 30 and 86400)",
            "The replay window must be between 30 seconds and one day.",
        ),
        (
            "body_limit_bounded",
            "check(max_body_bytes between 1024 and 16384)",
            "The body limit must be between 1 KiB and 16 KiB.",
        ),
        (
            "field_limit_bounded",
            "check(max_field_length between 64 and 512)",
            "The field limit must be between 64 and 512 characters.",
        ),
        (
            "rate_limit_bounded",
            "check(rate_limit_per_minute between 60 and 10000)",
            "The web ingress rate limit must be between 60 and 10,000 per minute.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        normalized = []
        for incoming in vals_list:
            values = dict(incoming)
            if {
                "public_ref",
                "public_key",
                "key_revision",
                "config_revision",
                "privacy_policy_set_by",
                "privacy_policy_set_at",
            }.intersection(values):
                raise AccessError(_("Web ingress identity is managed internally."))
            values.update(self._normalized_allowlists(values))
            if self._privacy_policy_fields().intersection(values):
                values.update(self._privacy_policy_audit_values())
            normalized.append(values)
        return super().create(normalized)

    def write(self, values):
        self.check_access_rights("write")
        self.check_access_rule("write")
        values = dict(values)
        internal = (
            self.env.context.get("marketing_web_ingress_internal")
            is WEB_INGRESS_INTERNAL_TOKEN
        )
        identity_fields = {
            "public_ref",
            "public_key",
            "key_revision",
            "config_revision",
            "privacy_policy_set_by",
            "privacy_policy_set_at",
        }
        if identity_fields.intersection(values) and not internal:
            raise AccessError(_("Web ingress identity is managed internally."))
        if "company_id" in values and any(
            endpoint.company_id.id != values["company_id"] for endpoint in self
        ):
            raise AccessError(
                _("A web ingress endpoint cannot move to another company.")
            )
        if not internal:
            values.update(self._normalized_allowlists(values))
        security_fields = {
            "active",
            "allowed_origins",
            "allowed_hosts",
            "replay_window_seconds",
            "max_body_bytes",
            "max_field_length",
            "rate_limit_per_minute",
        } | self._privacy_policy_fields()
        if not internal and self._privacy_policy_fields().intersection(values):
            values.update(self._privacy_policy_audit_values())
        if not internal and security_fields.intersection(values):
            for endpoint in self.sorted("id"):
                self.env.cr.execute(
                    "SELECT id FROM marketing_web_ingress_endpoint "
                    "WHERE id = %s FOR UPDATE",
                    [endpoint.id],
                )
                endpoint.invalidate_recordset(["config_revision"])
                item_values = dict(values, config_revision=endpoint.config_revision + 1)
                super(
                    MarketingWebIngressEndpoint,
                    endpoint.with_context(
                        marketing_web_ingress_internal=WEB_INGRESS_INTERNAL_TOKEN
                    ),
                ).write(item_values)
            return True
        return super().write(values)

    @api.model
    def _privacy_policy_fields(self):
        return {
            "capture_enabled",
            "capture_purpose",
            "privacy_policy_version",
            "privacy_notice_version",
            "privacy_legal_basis_code",
            "privacy_policy_justification",
            "identifier_retention_days",
        }

    @api.model
    def _privacy_policy_audit_values(self):
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_admin"
        ):
            raise AccessError(
                _("Only a Marketing Administrator can configure capture.")
            )
        return {
            "privacy_policy_set_by": self.env.uid,
            "privacy_policy_set_at": fields.Datetime.now(),
        }

    def _privacy_policy_configured(self):
        self.ensure_one()
        return bool(
            self.capture_purpose
            and (self.privacy_policy_version or "").strip()
            and (self.privacy_notice_version or "").strip()
            and self.privacy_legal_basis_code
            and (self.privacy_policy_justification or "").strip()
            and self.identifier_retention_days > 0
            and self.privacy_legal_basis_code.lower() not in {"consent", "granted"}
        )

    def _capture_policy_allows(self):
        self.ensure_one()
        return self.capture_enabled and self._privacy_policy_configured()

    @api.constrains(
        "capture_enabled",
        "capture_purpose",
        "privacy_policy_version",
        "privacy_notice_version",
        "privacy_legal_basis_code",
        "privacy_policy_justification",
        "identifier_retention_days",
    )
    def _check_privacy_policy(self):
        for endpoint in self:
            if endpoint.identifier_retention_days < 0:
                raise ValidationError(_("Retention days cannot be negative."))
            if endpoint.identifier_retention_days:
                endpoint._retention_deadline(fields.Datetime.now())
            for value in (endpoint.capture_purpose, endpoint.privacy_legal_basis_code):
                if value and not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value
                ):
                    raise ValidationError(
                        _("Use a bounded policy code without spaces.")
                    )
            if any(
                len(value or "") > 128
                for value in (
                    endpoint.privacy_policy_version,
                    endpoint.privacy_notice_version,
                )
            ):
                raise ValidationError(
                    _("Policy and notice versions are limited to 128 characters.")
                )
            if endpoint.capture_enabled and not endpoint._privacy_policy_configured():
                raise ValidationError(
                    _(
                        "Configure a purpose, policy and notice versions, documented "
                        "non-consent basis, justification and retention "
                        "before enabling capture."
                    )
                )

    def _retention_deadline(self, observed_at):
        self.ensure_one()
        try:
            return fields.Datetime.to_datetime(observed_at) + datetime.timedelta(
                days=self.identifier_retention_days
            )
        except OverflowError as error:
            raise ValidationError(
                _("The configured retention duration is too large.")
            ) from error

    def _compute_legacy_retention_count(self):
        for endpoint in self:
            endpoint.legacy_retention_count = self.env[
                "marketing.web.ingress.event"
            ].search_count(
                [
                    ("endpoint_id", "=", endpoint.id),
                    ("retain_until", "=", False),
                ]
            )

    def action_preview_legacy_retention(self):
        self.ensure_one()
        self.check_access_rights("write")
        self.check_access_rule("write")
        return {
            "type": "ir.actions.act_window",
            "name": _("Events without a retention policy"),
            "res_model": "marketing.web.ingress.event",
            "view_mode": "tree,form",
            "domain": [("endpoint_id", "=", self.id), ("retain_until", "=", False)],
        }

    def action_apply_legacy_retention(self):
        """Explicit operator action; never guess a duration for legacy evidence."""
        self.ensure_one()
        self.check_access_rights("write")
        self.check_access_rule("write")
        self._privacy_policy_audit_values()
        if not self._privacy_policy_configured():
            raise ValidationError(
                _("Configure the documented policy first; capture can remain disabled.")
            )
        self.env.cr.execute(
            "SELECT id FROM marketing_web_ingress_endpoint WHERE id = %s FOR SHARE",
            [self.id],
        )
        self.invalidate_recordset(list(self._privacy_policy_fields()))
        if not self._privacy_policy_configured():
            raise ValidationError(_("The capture policy changed; review it again."))
        events = self.env["marketing.web.ingress.event"].search(
            [
                ("endpoint_id", "=", self.id),
                ("retain_until", "=", False),
            ],
            order="id",
            limit=100,
        )
        for event in events:
            event._assign_retention_policy(self, assigned_by=self.env.uid)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "sticky": True,
                "message": _(
                    "Retention assigned to %(count)s legacy events. Expired values are "
                    "eligible for the next cleanup; review and repeat for remaining batches.",
                    count=len(events),
                ),
            },
        }

    @api.model
    def _normalized_allowlists(self, values):
        normalized = {}
        try:
            if "allowed_origins" in values:
                normalized["allowed_origins"] = "\n".join(
                    normalize_allowed_origins(values["allowed_origins"])
                )
            if "allowed_hosts" in values:
                normalized["allowed_hosts"] = "\n".join(
                    normalize_allowed_hosts(values["allowed_hosts"])
                )
        except WebIngressContractError as error:
            raise ValidationError(
                _("Invalid web ingress allowlist: %s") % error
            ) from error
        return normalized

    @api.constrains(
        "allowed_origins",
        "allowed_hosts",
        "replay_window_seconds",
        "max_body_bytes",
        "max_field_length",
        "rate_limit_per_minute",
    )
    def _check_configuration(self):
        for endpoint in self:
            try:
                normalize_allowed_origins(endpoint.allowed_origins)
                normalize_allowed_hosts(endpoint.allowed_hosts)
            except WebIngressContractError as error:
                raise ValidationError(
                    _("Invalid web ingress configuration: %s") % error
                ) from error

    def action_rotate_public_key(self):
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_admin"
        ):
            raise AccessError(_("Only a Marketing Administrator can rotate this key."))
        self.check_access_rights("write")
        self.check_access_rule("write")
        for endpoint in self.sorted("id"):
            self.env.cr.execute(
                "SELECT id FROM marketing_web_ingress_endpoint WHERE id = %s FOR UPDATE",
                [endpoint.id],
            )
            endpoint.invalidate_recordset(["key_revision", "config_revision"])
            endpoint.with_context(
                marketing_web_ingress_internal=WEB_INGRESS_INTERNAL_TOKEN
            ).write(
                {
                    "public_key": secrets.token_urlsafe(32),
                    "key_revision": endpoint.key_revision + 1,
                    "config_revision": endpoint.config_revision + 1,
                }
            )
        return True

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Web ingress endpoints must be archived, not deleted."))

    def _locked_for_public_key(self, candidate, candidate_revision):
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM marketing_web_ingress_endpoint WHERE id = %s FOR SHARE",
            [self.id],
        )
        self.invalidate_recordset(
            [
                "active",
                "public_key",
                "key_revision",
                "config_revision",
                "allowed_origins",
                "allowed_hosts",
                "replay_window_seconds",
                "max_body_bytes",
                "max_field_length",
                "rate_limit_per_minute",
            ]
            + list(self._privacy_policy_fields())
        )
        if (
            not self.active
            or not self._capture_policy_allows()
            or not isinstance(candidate, str)
            or len(candidate) > 128
            or not isinstance(candidate_revision, str)
            or not candidate_revision.isascii()
            or not candidate_revision.isdigit()
            or len(candidate_revision) > 10
            or int(candidate_revision) != self.config_revision
        ):
            return False
        return secrets.compare_digest(candidate.encode(), self.public_key.encode())

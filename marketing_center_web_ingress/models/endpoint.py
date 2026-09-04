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
            }.intersection(values):
                raise AccessError(_("Web ingress identity is managed internally."))
            values.update(self._normalized_allowlists(values))
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
        }
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
        )
        if (
            not self.active
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

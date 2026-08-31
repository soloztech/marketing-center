import re
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.meta_api_base.services.signature import validate_graph_version

from ..services.tokens import (
    MARKETING_META_CONNECTION_TOKEN,
    MARKETING_META_PROFILE_RUNTIME_TOKEN,
)

_ENVIRONMENT_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_FILE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


def _uuid(_recordset):
    return str(uuid.uuid4())


class MarketingCenterMetaProfile(models.Model):
    _name = "marketing.center.meta.profile"
    _description = "Marketing Center Meta Reader Profile"
    _order = "company_id, name, id"
    _check_company_auto = True

    name = fields.Char(required=True, index=True)
    active = fields.Boolean(default=True, index=True)
    public_ref = fields.Char(
        required=True,
        default=_uuid,
        size=36,
        readonly=True,
        copy=False,
        index=True,
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
        ondelete="restrict",
    )
    external_app_id = fields.Char(required=True, size=64, index=True)
    graph_version = fields.Char(required=True, default="v26.0", size=16)
    credential_backend = fields.Selection(
        [("environment", "Environment"), ("file", "Mounted secret file")],
        required=True,
        default="environment",
    )
    app_secret_ref = fields.Char(
        required=True,
        size=128,
        groups="marketing_center_base.group_marketing_center_admin",
        help="Environment key or mounted secret filename; never the secret value.",
    )
    access_token_ref = fields.Char(
        required=True,
        size=128,
        groups="marketing_center_base.group_marketing_center_admin",
        help="Environment key or mounted secret filename; never the token value.",
    )
    required_scopes = fields.Char(
        required=True,
        default="ads_read",
        size=1024,
        help="Comma-separated read scopes that token validation must prove.",
    )
    profile_revision = fields.Integer(
        required=True,
        default=1,
        readonly=True,
        copy=False,
    )
    health_state = fields.Selection(
        [
            ("unknown", "Unknown"),
            ("healthy", "Healthy"),
            ("degraded", "Degraded"),
            ("unhealthy", "Unhealthy"),
        ],
        required=True,
        default="unknown",
        readonly=True,
        index=True,
    )
    verified_at = fields.Datetime(readonly=True, copy=False, index=True)
    last_error_class = fields.Char(readonly=True, copy=False)
    last_error_message = fields.Char(readonly=True, copy=False)
    verified_scopes_json = fields.Json(readonly=True, copy=False)
    token_type = fields.Char(readonly=True, copy=False, size=64)
    token_expires_at = fields.Datetime(readonly=True, copy=False)
    data_access_expires_at = fields.Datetime(readonly=True, copy=False)
    connection_ids = fields.One2many(
        "marketing.center.connection",
        "meta_profile_id",
        readonly=True,
    )

    _sql_constraints = [
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The Meta profile public reference must be unique.",
        ),
        (
            "revision_positive",
            "check(profile_revision > 0)",
            "The Meta profile revision must be positive.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        runtime_fields = self._runtime_fields()
        normalized = []
        for incoming in vals_list:
            values = dict(incoming)
            if "public_ref" in values:
                raise AccessError(
                    _("The Meta profile public reference is managed internally.")
                )
            if "profile_revision" in values:
                raise AccessError(_("The Meta profile revision is managed internally."))
            if runtime_fields.intersection(values) and not self._runtime_internal():
                raise AccessError(_("Meta profile health is maintained internally."))
            values["external_app_id"] = str(values.get("external_app_id") or "").strip()
            values["app_secret_ref"] = str(values.get("app_secret_ref") or "").strip()
            values["access_token_ref"] = str(
                values.get("access_token_ref") or ""
            ).strip()
            values["required_scopes"] = self._normalized_scopes_text(
                values.get("required_scopes", "ads_read")
            )
            normalized.append(values)
        return super().create(normalized)

    def write(self, values):
        values = dict(values)
        if "public_ref" in values:
            raise AccessError(_("The Meta profile public reference is immutable."))
        if "company_id" in values and any(
            profile.company_id.id != values["company_id"] for profile in self
        ):
            raise AccessError(_("A Meta profile cannot be moved to another company."))
        if "profile_revision" in values:
            raise AccessError(_("The Meta profile revision is managed internally."))
        if self._runtime_fields().intersection(values) and not self._runtime_internal():
            raise AccessError(_("Meta profile health is maintained internally."))
        for field_name in ("external_app_id", "app_secret_ref", "access_token_ref"):
            if field_name in values:
                values[field_name] = str(values[field_name] or "").strip()
        if "required_scopes" in values:
            values["required_scopes"] = self._normalized_scopes_text(
                values["required_scopes"]
            )
        revision_fields = {
            "active",
            "external_app_id",
            "graph_version",
            "credential_backend",
            "app_secret_ref",
            "access_token_ref",
            "required_scopes",
        }
        if not revision_fields.intersection(values):
            return super().write(values)
        values.update(
            {
                "health_state": "unknown",
                "verified_at": False,
                "last_error_class": False,
                "last_error_message": False,
                "verified_scopes_json": False,
                "token_type": False,
                "token_expires_at": False,
                "data_access_expires_at": False,
            }
        )
        for profile in self.sorted("id"):
            self.env.cr.execute(
                "SELECT id FROM marketing_center_meta_profile WHERE id = %s FOR UPDATE",
                [profile.id],
            )
            super(MarketingCenterMetaProfile, profile).write(values)
            self.env.cr.execute(
                "UPDATE marketing_center_meta_profile "
                "SET profile_revision = profile_revision + 1 WHERE id = %s",
                [profile.id],
            )
            profile.invalidate_recordset(["profile_revision"])
            profile.connection_ids.filtered(
                lambda connection: connection.active
                and connection.state not in {"paused", "disabled"}
            ).with_context(
                marketing_meta_connection_token=MARKETING_META_CONNECTION_TOKEN
            ).write(
                {"state": "paused"}
            )
        return True

    @api.model
    def _runtime_fields(self):
        return {
            "health_state",
            "verified_at",
            "last_error_class",
            "last_error_message",
            "verified_scopes_json",
            "token_type",
            "token_expires_at",
            "data_access_expires_at",
        }

    def _runtime_internal(self):
        return (
            self.env.context.get("marketing_meta_profile_runtime_token")
            is MARKETING_META_PROFILE_RUNTIME_TOKEN
        )

    @api.model
    def _normalized_scopes_text(self, value):
        if not isinstance(value, str):
            raise ValidationError(_("Meta required scopes must be text."))
        scopes = []
        for raw in value.split(","):
            scope = raw.strip().lower()
            if not scope or not _SCOPE_RE.fullmatch(scope):
                raise ValidationError(_("A Meta required scope is invalid."))
            if scope not in scopes:
                scopes.append(scope)
        return ",".join(sorted(scopes))

    def required_scope_keys(self):
        self.ensure_one()
        return tuple(self.required_scopes.split(","))

    @api.constrains(
        "external_app_id",
        "graph_version",
        "credential_backend",
        "app_secret_ref",
        "access_token_ref",
        "required_scopes",
    )
    def _check_configuration(self):
        for profile in self:
            if not profile.external_app_id.isdigit():
                raise ValidationError(_("The Meta App ID must contain only digits."))
            if not validate_graph_version(profile.graph_version):
                raise ValidationError(_("The Meta Graph version is invalid."))
            reference_pattern = (
                _ENVIRONMENT_REF_RE
                if profile.credential_backend == "environment"
                else _FILE_REF_RE
            )
            if not reference_pattern.fullmatch(profile.app_secret_ref or "") or not (
                reference_pattern.fullmatch(profile.access_token_ref or "")
            ):
                raise ValidationError(_("A Meta credential reference is invalid."))
            self._normalized_scopes_text(profile.required_scopes)

    def action_enqueue_validation(self):
        self.ensure_one()
        self.check_access_rights("write")
        self.check_access_rule("write")
        self.with_delay(
            identity_key="marketing_meta:validate:%s:%s"
            % (self.public_ref, self.profile_revision),
            max_retries=8,
            priority=20,
            description="Validate Meta reader profile %s" % self.public_ref,
        )._job_validate_read_profile(expected_profile_revision=self.profile_revision)
        return True

    def action_enqueue_discovery(self):
        self.ensure_one()
        self.check_access_rights("write")
        self.check_access_rule("write")
        self.with_delay(
            identity_key="marketing_meta:discover:%s:%s"
            % (self.public_ref, self.profile_revision),
            max_retries=8,
            priority=30,
            description="Discover Meta ad accounts %s" % self.public_ref,
        )._job_discover_read_sources(expected_profile_revision=self.profile_revision)
        return True

    def _job_validate_read_profile(self, expected_profile_revision):
        self.ensure_one()
        return self.env["marketing.center.meta.service"]._validate_profile(
            self,
            expected_profile_revision=expected_profile_revision,
        )

    def _job_discover_read_sources(self, expected_profile_revision):
        self.ensure_one()
        return self.env["marketing.center.meta.service"]._discover_sources(
            self,
            expected_profile_revision=expected_profile_revision,
        )

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Meta profiles must be archived instead of deleted."))

import re
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.meta_api_base.services.credentials import (
    MetaCredentialResolutionError,
    validate_secret_reference,
)

from ..services.tokens import (
    MARKETING_META_CONNECTION_TOKEN,
    MARKETING_META_PROFILE_RUNTIME_TOKEN,
)

_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_READER_SCOPES = {
    "ads_reader": ("ads_read",),
    "lead_reader": (
        "ads_management",
        "leads_retrieval",
        "pages_manage_ads",
        "pages_manage_metadata",
        "pages_read_engagement",
        "pages_show_list",
    ),
}


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
    meta_app_id = fields.Many2one(
        "meta.api.app",
        string="Meta App",
        required=True,
        index=True,
        check_company=True,
        ondelete="restrict",
        help="Shared Meta App identity, Graph version and App Secret reference.",
    )
    reader_kind = fields.Selection(
        [("ads_reader", "Ads reader"), ("lead_reader", "Lead Ads reader")],
        required=True,
        default="ads_reader",
        index=True,
        help=(
            "Ads and Lead Ads use separate least-privilege reader profiles. "
            "A Lead Ads profile retrieves submissions but cannot bind an Ads "
            "catalog connection."
        ),
    )
    credential_backend = fields.Selection(
        [("environment", "Environment"), ("file", "Mounted secret file")],
        required=True,
        default="environment",
        groups="base.group_system",
    )
    access_token_ref = fields.Char(
        required=True,
        size=128,
        groups="base.group_system",
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
    lead_route_ids = fields.One2many(
        "marketing.center.meta.lead.route",
        "lead_profile_id",
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
            self._check_secret_reference_authority(values)
            if "public_ref" in values:
                raise AccessError(
                    _("The Meta profile public reference is managed internally.")
                )
            if "profile_revision" in values:
                raise AccessError(_("The Meta profile revision is managed internally."))
            if runtime_fields.intersection(values) and not self._runtime_internal():
                raise AccessError(_("Meta profile health is maintained internally."))
            values["access_token_ref"] = str(
                values.get("access_token_ref") or ""
            ).strip()
            reader_kind = values.get("reader_kind", "ads_reader")
            values["required_scopes"] = self._normalized_scopes_text(
                values.get("required_scopes") or self._default_scopes_text(reader_kind)
            )
            normalized.append(values)
        return super().create(normalized)

    def write(self, values):
        values = dict(values)
        self._check_secret_reference_authority(values)
        if "public_ref" in values:
            raise AccessError(_("The Meta profile public reference is immutable."))
        if "company_id" in values and any(
            profile.company_id.id != values["company_id"] for profile in self
        ):
            raise AccessError(_("A Meta profile cannot be moved to another company."))
        if "profile_revision" in values:
            raise AccessError(_("The Meta profile revision is managed internally."))
        self._validate_reader_kind_change(values)
        if self._runtime_fields().intersection(values) and not self._runtime_internal():
            raise AccessError(_("Meta profile health is maintained internally."))
        if "access_token_ref" in values:
            values["access_token_ref"] = str(values["access_token_ref"] or "").strip()
        if "required_scopes" in values:
            values["required_scopes"] = self._normalized_scopes_text(
                values["required_scopes"]
            )
        revision_fields = (
            "active",
            "meta_app_id",
            "reader_kind",
            "credential_backend",
            "access_token_ref",
            "required_scopes",
        )
        if not set(revision_fields).intersection(values):
            return super().write(values)
        self.flush_recordset(["company_id", "profile_revision", *revision_fields])
        for profile in self.sorted("id"):
            self.env.cr.execute(
                "SELECT company_id, profile_revision, active, meta_app_id, "
                "reader_kind, credential_backend, access_token_ref, required_scopes "
                "FROM marketing_center_meta_profile WHERE id = %s FOR UPDATE",
                [profile.id],
            )
            locked = self.env.cr.fetchone()
            if not locked:
                raise AccessError(_("The Meta profile no longer exists."))
            company_id, current_revision, *configuration = locked
            if "company_id" in values and values["company_id"] != company_id:
                raise AccessError(
                    _("A Meta profile cannot be moved to another company.")
                )
            profile_values = dict(values)
            persisted = dict(zip(revision_fields, configuration))
            if (
                "reader_kind" in profile_values
                and "required_scopes" not in profile_values
                and profile_values["reader_kind"] != persisted["reader_kind"]
            ):
                profile_values["required_scopes"] = self._default_scopes_text(
                    profile_values["reader_kind"]
                )
            configuration_changed = any(
                field_name in profile_values
                and profile_values[field_name] != persisted[field_name]
                for field_name in revision_fields
            )
            if configuration_changed:
                profile_values.update(
                    {
                        "profile_revision": current_revision + 1,
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
            super(MarketingCenterMetaProfile, profile).write(profile_values)
            profile.flush_recordset(list(profile_values))
            profile.invalidate_recordset(["profile_revision"])
            if configuration_changed:
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
    def _check_secret_reference_authority(self, values):
        if {"credential_backend", "access_token_ref"}.intersection(
            values
        ) and not self.env.user.has_group("base.group_system"):
            raise AccessError(
                _("Only a Settings administrator can select Meta credentials.")
            )

    def _validate_reader_kind_change(self, values):
        if "reader_kind" not in values:
            return
        target_kind = values["reader_kind"]
        if target_kind != "lead_reader" and any(
            profile.lead_route_ids.filtered("active") for profile in self
        ):
            raise ValidationError(
                _("Archive Lead Ads routes before changing this reader kind.")
            )
        if target_kind != "ads_reader" and any(
            profile.connection_ids.filtered("active") for profile in self
        ):
            raise ValidationError(
                _("Archive Meta Ads connections before changing this reader kind.")
            )

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

    @api.model
    def _default_scopes_text(self, reader_kind):
        scopes = _READER_SCOPES.get(str(reader_kind or ""))
        if not scopes:
            raise ValidationError(_("The Meta reader kind is invalid."))
        return ",".join(scopes)

    @api.constrains(
        "company_id",
        "meta_app_id",
        "reader_kind",
        "credential_backend",
        "access_token_ref",
        "required_scopes",
    )
    def _check_configuration(self):
        for profile in self:
            if profile.meta_app_id.company_id != profile.company_id:
                raise ValidationError(
                    _(
                        "The Meta App and reader profile must belong to the same company."
                    )
                )
            try:
                validate_secret_reference(
                    profile.credential_backend,
                    profile.access_token_ref,
                )
            except MetaCredentialResolutionError:
                raise ValidationError(
                    _("The Meta access-token reference is invalid.")
                ) from None
            self._normalized_scopes_text(profile.required_scopes)
            minimum = set(_READER_SCOPES.get(profile.reader_kind, ()))
            if not minimum.issubset(profile.required_scope_keys()):
                raise ValidationError(
                    _("The Meta reader profile is missing required scopes.")
                )

    def action_enqueue_validation(self):
        self.ensure_one()
        self.check_access_rights("write")
        self.check_access_rule("write")
        self.with_delay(
            identity_key="marketing_meta:validate:%s:%s:%s"
            % (self.public_ref, self.profile_revision, self.meta_app_id.revision),
            max_retries=8,
            priority=20,
            description="Validate Meta reader profile %s" % self.public_ref,
        )._job_validate_read_profile(
            expected_profile_revision=self.profile_revision,
            expected_app_revision=self.meta_app_id.revision,
        )
        return True

    def action_enqueue_discovery(self):
        self.ensure_one()
        self.check_access_rights("write")
        self.check_access_rule("write")
        if self.reader_kind != "ads_reader":
            raise ValidationError(
                _("Lead Ads reader profiles do not discover ad accounts.")
            )
        self.with_delay(
            identity_key="marketing_meta:discover:%s:%s:%s"
            % (self.public_ref, self.profile_revision, self.meta_app_id.revision),
            max_retries=8,
            priority=30,
            description="Discover Meta ad accounts %s" % self.public_ref,
        )._job_discover_read_sources(
            expected_profile_revision=self.profile_revision,
            expected_app_revision=self.meta_app_id.revision,
        )
        return True

    def _job_validate_read_profile(
        self,
        expected_profile_revision,
        expected_app_revision,
    ):
        self.ensure_one()
        return self.env["marketing.center.meta.service"]._validate_profile(
            self,
            expected_profile_revision=expected_profile_revision,
            expected_app_revision=expected_app_revision,
        )

    def _job_discover_read_sources(
        self,
        expected_profile_revision,
        expected_app_revision,
    ):
        self.ensure_one()
        return self.env["marketing.center.meta.service"]._discover_sources(
            self,
            expected_profile_revision=expected_profile_revision,
            expected_app_revision=expected_app_revision,
        )

    def _queue_job_attempt_is_terminal(self, expected_method):
        """Return whether the running OCA job is on its last allowed try.

        ``queue_job`` persists the completed-try counter before ``Job.perform``
        increments its in-memory value.  Consequently a stored retry of seven
        is the eighth (terminal) execution when ``max_retries`` is eight.
        Bind the observation to this exact profile and method so a forged or
        stale ``job_uuid`` cannot project health on another configuration.
        """

        self.ensure_one()
        allowed_methods = {
            "_job_validate_read_profile",
            "_job_discover_read_sources",
        }
        if expected_method not in allowed_methods:
            return False
        job_uuid = self.env.context.get("job_uuid")
        if not job_uuid:
            return False
        job = (
            self.env["queue.job"].sudo().search([("uuid", "=", str(job_uuid))], limit=1)
        )
        if (
            not job
            or job.state != "started"
            or job.model_name != self._name
            or job.method_name != expected_method
            or not job.max_retries
            or job.retry + 1 < job.max_retries
        ):
            return False
        records = job.records
        return bool(
            records
            and getattr(records, "_name", "") == self._name
            and records.exists().ids == self.ids
        )

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Meta profiles must be archived instead of deleted."))

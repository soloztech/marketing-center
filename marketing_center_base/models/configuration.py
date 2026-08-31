import re
import uuid

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.tokens import (
    MARKETING_CONFIGURATION_RUNTIME_TOKEN,
    MARKETING_WRITE_CAPABILITY_TOKEN,
)

_TECHNICAL_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")


def _uuid(_recordset):
    return str(uuid.uuid4())


def _normalize_text(value):
    return value.strip() if isinstance(value, str) else value


def _normalize_key(value):
    value = _normalize_text(value)
    return value.lower() if isinstance(value, str) else value


class MarketingCenterTeam(models.Model):
    _name = "marketing.center.team"
    _description = "Marketing Center Team"
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
    member_ids = fields.One2many(
        "marketing.center.team.member", "team_id", string="Members"
    )
    source_link_ids = fields.One2many(
        "marketing.center.team.source", "team_id", string="Sources"
    )
    access_user_ids = fields.Many2many(
        "res.users",
        "marketing_center_team_access_user_rel",
        "team_id",
        "user_id",
        compute="_compute_access_user_ids",
        store=True,
        compute_sudo=True,
        string="Active Access Users",
        groups="marketing_center_base.group_marketing_center_admin",
    )

    _sql_constraints = [
        (
            "company_name_unique",
            "unique(company_id, name)",
            "A marketing team with this name already exists in the company.",
        )
    ]

    @api.depends("active", "member_ids.active", "member_ids.user_id")
    def _compute_access_user_ids(self):
        for team in self:
            team.access_user_ids = (
                team.member_ids.filtered("active").mapped("user_id")
                if team.active
                else self.env["res.users"]
            )

    def write(self, values):
        if "company_id" in values and any(
            team.company_id.id != values["company_id"] for team in self
        ):
            raise AccessError(_("A marketing team cannot be moved to another company."))
        return super().write(values)

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Marketing teams must be archived instead of deleted."))


class MarketingCenterTeamMember(models.Model):
    _name = "marketing.center.team.member"
    _description = "Marketing Center Team Member"
    _order = "team_id, role, user_id, id"
    _check_company_auto = True

    active = fields.Boolean(default=True, index=True)
    team_id = fields.Many2one(
        "marketing.center.team",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    company_id = fields.Many2one(
        related="team_id.company_id", store=True, readonly=True, index=True
    )
    user_id = fields.Many2one(
        "res.users", required=True, index=True, ondelete="restrict"
    )
    role = fields.Selection(
        [
            ("viewer", "Viewer"),
            ("analyst", "Analyst"),
            ("operator", "Operator"),
            ("manager", "Manager"),
        ],
        required=True,
        default="viewer",
        index=True,
    )

    _sql_constraints = [
        (
            "team_user_unique",
            "unique(team_id, user_id)",
            "This user is already a member of the marketing team.",
        )
    ]

    @api.constrains("team_id", "user_id")
    def _check_user_company(self):
        for member in self:
            if member.team_id.company_id not in member.user_id.company_ids:
                raise ValidationError(
                    _("The team company must be available to the selected user.")
                )

    def write(self, values):
        for field_name in ("team_id", "user_id"):
            if field_name in values and any(
                member[field_name].id != values[field_name] for member in self
            ):
                raise AccessError(
                    _(
                        "A team membership identity cannot be changed; archive it instead."
                    )
                )
        return super().write(values)

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Marketing team members must be archived instead."))


class MarketingCenterTeamSource(models.Model):
    _name = "marketing.center.team.source"
    _description = "Marketing Center Team Source Access"
    _order = "team_id, source_id, id"
    _check_company_auto = True

    active = fields.Boolean(default=True, index=True)
    team_id = fields.Many2one(
        "marketing.center.team",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    source_id = fields.Many2one(
        "marketing.center.source",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    company_id = fields.Many2one(
        related="team_id.company_id", store=True, readonly=True, index=True
    )
    access_mode = fields.Selection(
        [
            ("read", "Read"),
            ("prepare", "Prepare"),
            ("operate", "Operate"),
            ("approve", "Approve"),
        ],
        required=True,
        default="read",
        index=True,
    )

    _sql_constraints = [
        (
            "team_source_unique",
            "unique(team_id, source_id)",
            "This source is already assigned to the marketing team.",
        )
    ]

    @api.constrains("team_id", "source_id")
    def _check_same_company(self):
        for link in self:
            if link.team_id.company_id != link.source_id.company_id:
                raise ValidationError(
                    _("A marketing team cannot access a source from another company.")
                )

    def write(self, values):
        for field_name in ("team_id", "source_id"):
            if field_name in values and any(
                link[field_name].id != values[field_name] for link in self
            ):
                raise AccessError(
                    _(
                        "A source assignment identity cannot be changed; archive it instead."
                    )
                )
        return super().write(values)

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Marketing source assignments must be archived instead."))


class MarketingCenterSource(models.Model):
    _name = "marketing.center.source"
    _description = "Marketing Center Source"
    _order = "company_id, service, name, id"
    _check_company_auto = True

    name = fields.Char(required=True, index=True)
    active = fields.Boolean(default=True, index=True)
    public_ref = fields.Char(
        required=True,
        default=_uuid,
        size=36,
        index=True,
        readonly=True,
        copy=False,
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
        ondelete="restrict",
    )
    service = fields.Char(
        required=True,
        size=128,
        index=True,
        help="Provider-neutral service key registered by an integration addon.",
    )
    external_account_ref = fields.Char(
        required=True,
        size=512,
        index=True,
        help="Case-sensitive external account or property reference.",
    )
    external_account_id = fields.Char(size=256, index=True)
    currency_id = fields.Many2one(
        "res.currency",
        required=True,
        default=lambda self: self.env.company.currency_id,
        ondelete="restrict",
    )
    timezone = fields.Char(required=True, default="UTC", size=64)
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("active", "Active"),
            ("paused", "Paused"),
            ("attention", "Attention"),
            ("disabled", "Disabled"),
        ],
        required=True,
        default="draft",
        index=True,
    )
    read_enabled = fields.Boolean(default=True, index=True)
    write_enabled = fields.Boolean(
        default=False,
        index=True,
        help="External mutations remain disabled until an explicit policy enables them.",
    )
    effective_capabilities_json = fields.Json(
        readonly=True,
        copy=False,
        default=dict,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    configuration_revision = fields.Integer(
        required=True, default=1, readonly=True, copy=False
    )
    first_observed_at = fields.Datetime(readonly=True, copy=False)
    last_observed_at = fields.Datetime(readonly=True, copy=False, index=True)
    connection_ids = fields.One2many(
        "marketing.center.connection",
        "source_id",
        readonly=True,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    team_source_ids = fields.One2many(
        "marketing.center.team.source", "source_id", string="Team Access"
    )
    access_user_ids = fields.Many2many(
        "res.users",
        "marketing_center_source_access_user_rel",
        "source_id",
        "user_id",
        compute="_compute_access_user_ids",
        store=True,
        compute_sudo=True,
        string="Active Access Users",
        groups="marketing_center_base.group_marketing_center_admin",
    )

    _sql_constraints = [
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The marketing source public reference must be unique.",
        ),
        (
            "account_scope_unique",
            "unique(company_id, service, external_account_ref)",
            "This external marketing account already exists in the company.",
        ),
        (
            "configuration_revision_positive",
            "check(configuration_revision > 0)",
            "The marketing source configuration revision must be positive.",
        ),
    ]

    @api.depends(
        "team_source_ids.active",
        "team_source_ids.team_id.access_user_ids",
    )
    def _compute_access_user_ids(self):
        for source in self:
            source.access_user_ids = source.team_source_ids.filtered("active").mapped(
                "team_id.access_user_ids"
            )

    @api.model_create_multi
    def create(self, vals_list):
        normalized = []
        for incoming in vals_list:
            values = dict(incoming)
            if "configuration_revision" in values:
                raise AccessError(
                    _("The configuration revision is managed internally.")
                )
            runtime_fields = {
                "effective_capabilities_json",
                "first_observed_at",
                "last_observed_at",
            }
            if runtime_fields.intersection(values) and (
                self.env.context.get("marketing_configuration_runtime_token")
                is not MARKETING_CONFIGURATION_RUNTIME_TOKEN
            ):
                raise AccessError(_("Source runtime state is maintained internally."))
            values["service"] = _normalize_key(values.get("service"))
            values["external_account_ref"] = _normalize_text(
                values.get("external_account_ref")
            )
            values["external_account_id"] = _normalize_text(
                values.get("external_account_id")
            )
            if values.get("write_enabled") and (
                self.env.context.get("marketing_write_capability_token")
                is not MARKETING_WRITE_CAPABILITY_TOKEN
            ):
                raise AccessError(
                    _("Marketing write capabilities are not enabled in this release.")
                )
            if values.get("effective_capabilities_json") and (
                self.env.context.get("marketing_configuration_runtime_token")
                is not MARKETING_CONFIGURATION_RUNTIME_TOKEN
            ):
                raise AccessError(_("Capabilities are maintained by integration code."))
            normalized.append(values)
        return super().create(normalized)

    def write(self, values):
        values = dict(values)
        if "company_id" in values and any(
            source.company_id.id != values["company_id"] for source in self
        ):
            raise AccessError(
                _("A marketing source cannot be moved to another company.")
            )
        if "configuration_revision" in values:
            raise AccessError(_("The configuration revision is managed internally."))
        runtime_fields = {
            "effective_capabilities_json",
            "first_observed_at",
            "last_observed_at",
        }
        if runtime_fields.intersection(values) and (
            self.env.context.get("marketing_configuration_runtime_token")
            is not MARKETING_CONFIGURATION_RUNTIME_TOKEN
        ):
            raise AccessError(_("Source runtime state is maintained internally."))
        if values.get("write_enabled") and (
            self.env.context.get("marketing_write_capability_token")
            is not MARKETING_WRITE_CAPABILITY_TOKEN
        ):
            raise AccessError(
                _("Marketing write capabilities are not enabled in this release.")
            )
        if "effective_capabilities_json" in values and (
            self.env.context.get("marketing_configuration_runtime_token")
            is not MARKETING_CONFIGURATION_RUNTIME_TOKEN
        ):
            raise AccessError(_("Capabilities are maintained by integration code."))
        if "service" in values:
            values["service"] = _normalize_key(values["service"])
        for field_name in ("external_account_ref", "external_account_id"):
            if field_name in values:
                values[field_name] = _normalize_text(values[field_name])
        revision_fields = {
            "service",
            "external_account_ref",
            "currency_id",
            "timezone",
            "state",
            "read_enabled",
            "write_enabled",
            "effective_capabilities_json",
            "active",
        }
        if revision_fields.intersection(values):
            for source in self.sorted("id"):
                self.env.cr.execute(
                    "SELECT id FROM marketing_center_source WHERE id = %s FOR UPDATE",
                    [source.id],
                )
                super(MarketingCenterSource, source).write(values)
                self.env.cr.execute(
                    "UPDATE marketing_center_source "
                    "SET configuration_revision = configuration_revision + 1 "
                    "WHERE id = %s",
                    [source.id],
                )
                source.invalidate_recordset(["configuration_revision"])
            return True
        return super().write(values)

    @api.constrains("service", "external_account_ref", "timezone")
    def _check_configuration_values(self):
        for source in self:
            if not _TECHNICAL_KEY_RE.fullmatch(source.service or ""):
                raise ValidationError(_("The marketing service key is invalid."))
            if not source.external_account_ref or any(
                ord(character) < 32 for character in source.external_account_ref
            ):
                raise ValidationError(
                    _("The external marketing account reference is invalid.")
                )
            try:
                pytz.timezone(source.timezone)
            except pytz.UnknownTimeZoneError as error:
                raise ValidationError(
                    _("The source timezone must be a valid IANA timezone.")
                ) from error

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Marketing sources must be archived instead of deleted."))


class MarketingCenterConnection(models.Model):
    _name = "marketing.center.connection"
    _description = "Marketing Center Technical Connection"
    _order = "source_id, purpose, adapter_key, id"
    _check_company_auto = True

    name = fields.Char(required=True)
    active = fields.Boolean(default=True, index=True)
    public_ref = fields.Char(
        required=True,
        default=_uuid,
        size=36,
        index=True,
        readonly=True,
        copy=False,
    )
    source_id = fields.Many2one(
        "marketing.center.source",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    company_id = fields.Many2one(
        related="source_id.company_id", store=True, readonly=True, index=True
    )
    adapter_key = fields.Char(required=True, size=128, index=True)
    purpose = fields.Selection(
        [
            ("reader", "Reader"),
            ("writer", "Writer"),
            ("webhook", "Webhook"),
            ("conversion", "Conversion delivery"),
        ],
        required=True,
        default="reader",
        index=True,
    )
    profile_public_ref = fields.Char(
        required=True,
        size=256,
        groups="marketing_center_base.group_marketing_center_admin",
        help="Opaque public reference resolved by the technical integration addon.",
    )
    profile_revision = fields.Integer(
        required=True,
        default=0,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    binding_revision = fields.Integer(required=True, default=1, readonly=True)
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("ready", "Ready"),
            ("degraded", "Degraded"),
            ("paused", "Paused"),
            ("error", "Error"),
            ("disabled", "Disabled"),
        ],
        required=True,
        default="draft",
        index=True,
    )
    effective_capabilities_json = fields.Json(
        readonly=True,
        copy=False,
        default=dict,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    effective_capabilities_hash = fields.Char(
        size=64,
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_admin",
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
    verified_at = fields.Datetime(readonly=True, copy=False)
    cooldown_until = fields.Datetime(readonly=True, copy=False, index=True)
    last_health_error_class = fields.Char(
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    last_health_error_message = fields.Char(
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_admin",
    )

    _sql_constraints = [
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The marketing connection public reference must be unique.",
        ),
        (
            "source_adapter_purpose_unique",
            "unique(source_id, adapter_key, purpose)",
            "A connection already exists for this source, adapter and purpose.",
        ),
        (
            "revisions_nonnegative",
            "check(binding_revision > 0 and profile_revision >= 0)",
            "Marketing connection revisions are invalid.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        normalized = []
        for incoming in vals_list:
            values = dict(incoming)
            if "binding_revision" in values:
                raise AccessError(_("The connection revision is managed internally."))
            runtime_fields = {
                "effective_capabilities_json",
                "effective_capabilities_hash",
                "health_state",
                "verified_at",
                "cooldown_until",
                "last_health_error_class",
                "last_health_error_message",
            }
            if runtime_fields.intersection(values) and (
                self.env.context.get("marketing_configuration_runtime_token")
                is not MARKETING_CONFIGURATION_RUNTIME_TOKEN
            ):
                raise AccessError(
                    _("Connection runtime state is maintained internally.")
                )
            capability_fields = {
                "effective_capabilities_json",
                "effective_capabilities_hash",
            }
            if capability_fields.intersection(
                values
            ) and not capability_fields.issubset(values):
                raise ValidationError(
                    _("Connection capabilities and their hash must be stored together.")
                )
            values["adapter_key"] = _normalize_key(values.get("adapter_key"))
            values["profile_public_ref"] = _normalize_text(
                values.get("profile_public_ref")
            )
            if values.get("effective_capabilities_json") and (
                self.env.context.get("marketing_configuration_runtime_token")
                is not MARKETING_CONFIGURATION_RUNTIME_TOKEN
            ):
                raise AccessError(_("Capabilities are maintained by integration code."))
            normalized.append(values)
        return super().create(normalized)

    def write(self, values):
        values = dict(values)
        if "source_id" in values and any(
            connection.source_id.id != values["source_id"] for connection in self
        ):
            raise AccessError(
                _("A marketing connection cannot be moved to another source.")
            )
        if "binding_revision" in values:
            raise AccessError(_("The connection revision is managed internally."))
        runtime_fields = {
            "effective_capabilities_json",
            "effective_capabilities_hash",
            "health_state",
            "verified_at",
            "cooldown_until",
            "last_health_error_class",
            "last_health_error_message",
        }
        if runtime_fields.intersection(values) and (
            self.env.context.get("marketing_configuration_runtime_token")
            is not MARKETING_CONFIGURATION_RUNTIME_TOKEN
        ):
            raise AccessError(_("Connection runtime state is maintained internally."))
        capability_fields = {
            "effective_capabilities_json",
            "effective_capabilities_hash",
        }
        if capability_fields.intersection(values) and not capability_fields.issubset(
            values
        ):
            raise ValidationError(
                _("Connection capabilities and their hash must be stored together.")
            )
        if "adapter_key" in values:
            values["adapter_key"] = _normalize_key(values["adapter_key"])
        if "profile_public_ref" in values:
            values["profile_public_ref"] = _normalize_text(values["profile_public_ref"])
        revision_fields = {
            "adapter_key",
            "purpose",
            "profile_public_ref",
            "profile_revision",
            "state",
            "effective_capabilities_hash",
            "active",
        }
        if revision_fields.intersection(values):
            for connection in self.sorted("id"):
                self.env.cr.execute(
                    "SELECT id FROM marketing_center_connection "
                    "WHERE id = %s FOR UPDATE",
                    [connection.id],
                )
                super(MarketingCenterConnection, connection).write(values)
                self.env.cr.execute(
                    "UPDATE marketing_center_connection "
                    "SET binding_revision = binding_revision + 1 WHERE id = %s",
                    [connection.id],
                )
                connection.invalidate_recordset(["binding_revision"])
            return True
        return super().write(values)

    @api.constrains("adapter_key", "profile_public_ref", "effective_capabilities_hash")
    def _check_technical_references(self):
        for connection in self:
            if not _TECHNICAL_KEY_RE.fullmatch(connection.adapter_key or ""):
                raise ValidationError(_("The marketing adapter key is invalid."))
            if not connection.profile_public_ref or any(
                ord(character) < 32 for character in connection.profile_public_ref
            ):
                raise ValidationError(_("The technical profile reference is invalid."))
            if connection.effective_capabilities_hash and (
                len(connection.effective_capabilities_hash) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in connection.effective_capabilities_hash
                )
            ):
                raise ValidationError(
                    _("The effective capabilities hash must be a SHA-256 digest.")
                )

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(
            _("Marketing connections must be archived instead of deleted.")
        )

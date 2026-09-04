import re
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.credentials import (
    MetaCredentialResolutionError,
    MetaRuntimeApp,
    resolve_secret,
    validate_secret_reference,
)
from ..services.signature import META_GRAPH_BASELINE_VERSION, validate_graph_version
from ..services.tokens import META_API_RUNTIME_CONTEXT_KEY, META_API_RUNTIME_TOKEN

_META_APP_ID_RE = re.compile(r"^[0-9]{1,64}$")


def _uuid(_recordset):
    return str(uuid.uuid4())


class MetaApiApp(models.Model):
    _name = "meta.api.app"
    _description = "Shared Meta API App"
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
    external_app_id = fields.Char(
        string="Meta App ID",
        required=True,
        size=64,
        index=True,
    )
    graph_version = fields.Char(
        required=True,
        default=META_GRAPH_BASELINE_VERSION,
        size=16,
    )
    credential_backend = fields.Selection(
        [("environment", "Environment"), ("file", "Mounted secret file")],
        required=True,
        default="environment",
    )
    app_secret_ref = fields.Char(
        required=True,
        size=128,
        groups="base.group_system",
        help="Environment key or mounted secret filename; never the secret value.",
    )
    revision = fields.Integer(
        required=True,
        default=1,
        readonly=True,
        copy=False,
        index=True,
        help="Monotonic configuration fence for asynchronous consumers.",
    )

    _sql_constraints = [
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The shared Meta App public reference must be unique.",
        ),
        (
            "company_external_app_unique",
            "unique(company_id, external_app_id)",
            "This Meta App already exists for the company.",
        ),
        (
            "revision_positive",
            "check(revision > 0)",
            "The shared Meta App revision must be positive.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        normalized = []
        for incoming in vals_list:
            values = dict(incoming)
            if "public_ref" in values:
                raise AccessError(
                    _("The shared Meta App public reference is managed internally.")
                )
            if "revision" in values:
                raise AccessError(
                    _("The shared Meta App revision is managed internally.")
                )
            for field_name in ("external_app_id", "graph_version", "app_secret_ref"):
                if field_name in values:
                    values[field_name] = str(values[field_name] or "").strip()
            normalized.append(values)
        return super().create(normalized)

    def write(self, values):
        values = dict(values)
        if "public_ref" in values:
            raise AccessError(_("The shared Meta App public reference is immutable."))
        if "revision" in values:
            raise AccessError(_("The shared Meta App revision is managed internally."))
        for field_name in ("external_app_id", "graph_version", "app_secret_ref"):
            if field_name in values:
                values[field_name] = str(values[field_name] or "").strip()
        revision_fields = {
            "active",
            "external_app_id",
            "graph_version",
            "credential_backend",
            "app_secret_ref",
        }
        # The lock below reads the revision with raw SQL.  Flush the ORM first:
        # stored writes may still be pending, and fencing must never calculate a
        # new revision from a stale database value.
        self.flush_recordset(["company_id", "revision"])
        for app in self.sorted("id"):
            self.env.cr.execute(
                "SELECT company_id, revision, active, external_app_id, "
                "graph_version, credential_backend, app_secret_ref "
                "FROM meta_api_app WHERE id = %s FOR UPDATE",
                [app.id],
            )
            locked = self.env.cr.fetchone()
            if not locked:
                raise AccessError(_("The shared Meta App no longer exists."))
            company_id, current_revision, *configuration = locked
            if "company_id" in values and values["company_id"] != company_id:
                raise AccessError(
                    _("A shared Meta App cannot be moved to another company.")
                )
            app_values = dict(values)
            persisted = dict(
                zip(
                    (
                        "active",
                        "external_app_id",
                        "graph_version",
                        "credential_backend",
                        "app_secret_ref",
                    ),
                    configuration,
                )
            )
            if any(
                field_name in values and values[field_name] != persisted[field_name]
                for field_name in revision_fields
            ):
                app_values["revision"] = current_revision + 1
            super(MetaApiApp, app).write(app_values)
            # ``_resolve_runtime`` deliberately uses a locked SQL snapshot. Make
            # the just-written configuration visible to that snapshot and drop
            # the possibly prefetched revision so callers observe the same fence.
            app.flush_recordset(list(app_values))
            app.invalidate_recordset(["revision"])
        return True

    @api.constrains(
        "external_app_id",
        "graph_version",
        "credential_backend",
        "app_secret_ref",
    )
    def _check_configuration(self):
        for app in self:
            if not _META_APP_ID_RE.fullmatch(app.external_app_id or ""):
                raise ValidationError(_("Meta App ID must contain only digits."))
            if not validate_graph_version(app.graph_version):
                raise ValidationError(_("Meta Graph version is invalid."))
            try:
                validate_secret_reference(
                    app.credential_backend,
                    app.app_secret_ref,
                )
            except MetaCredentialResolutionError:
                raise ValidationError(
                    _("The Meta App secret reference is invalid.")
                ) from None

    def _resolve_runtime(self, expected_revision=None):
        """Resolve one fenced runtime App; the secret exists only in memory."""

        self.ensure_one()
        if (
            self.env.context.get(META_API_RUNTIME_CONTEXT_KEY)
            is not META_API_RUNTIME_TOKEN
        ):
            raise AccessError(_("Meta App runtime is available only server-side."))
        self.check_access_rights("read")
        self.check_access_rule("read")
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision <= 0
        ):
            raise MetaCredentialResolutionError("Meta App expected revision is invalid")
        # Raw SQL is used to obtain one coherent, locked runtime snapshot.  It
        # must be preceded by a targeted flush or it can see the previous
        # revision/configuration while the ORM cache already exposes the new one.
        self.flush_recordset(
            [
                "active",
                "company_id",
                "public_ref",
                "external_app_id",
                "graph_version",
                "credential_backend",
                "app_secret_ref",
                "revision",
            ]
        )
        self.env.cr.execute(
            """
            SELECT active,
                   company_id,
                   public_ref,
                   external_app_id,
                   graph_version,
                   credential_backend,
                   app_secret_ref,
                   revision
              FROM meta_api_app
             WHERE id = %s
             FOR SHARE
            """,
            [self.id],
        )
        locked = self.env.cr.fetchone()
        if not locked:
            raise MetaCredentialResolutionError("Meta App is unavailable")
        (
            active,
            company_id,
            public_ref,
            external_app_id,
            graph_version,
            credential_backend,
            app_secret_ref,
            revision,
        ) = locked
        if expected_revision is not None and expected_revision != revision:
            raise MetaCredentialResolutionError("Meta App configuration changed")
        if not active:
            raise MetaCredentialResolutionError("Meta App is paused")
        app_secret = resolve_secret(credential_backend, app_secret_ref)
        return MetaRuntimeApp(
            active=active,
            external_app_id=external_app_id,
            graph_version=graph_version,
            app_secret=app_secret,
            public_ref=public_ref,
            revision=revision,
            company_id=company_id,
        )

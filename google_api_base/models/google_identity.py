import re
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.credentials import (
    GoogleCredentialResolutionError,
    GoogleRuntimeIdentity,
    resolve_credential,
    resolve_service_account_info,
    validate_credential_reference,
)
from ..services.tokens import GOOGLE_API_RUNTIME_CONTEXT_KEY, GOOGLE_API_RUNTIME_TOKEN
from ..services.version import GOOGLE_ADS_BASELINE_VERSION, validate_api_version

_CUSTOMER_ID_RE = re.compile(r"^[0-9]{10}$")


def _uuid(_recordset):
    return str(uuid.uuid4())


def _customer_id(value):
    return str(value or "").strip().replace("-", "")


class GoogleApiIdentity(models.Model):
    _name = "google.api.identity"
    _description = "Shared Google API Identity"
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
    api_version = fields.Char(
        required=True,
        default=GOOGLE_ADS_BASELINE_VERSION,
        size=8,
    )
    auth_mode = fields.Selection(
        [
            ("authorized_user", "OAuth authorized user"),
            ("service_account", "Service account"),
        ],
        required=True,
        default="authorized_user",
    )
    credential_backend = fields.Selection(
        [("environment", "Environment"), ("file", "Mounted secret file")],
        required=True,
        default="environment",
    )
    oauth_client_id_ref = fields.Char(
        size=128,
        groups="base.group_system",
        help="Environment key or mounted filename; never the OAuth client ID.",
    )
    oauth_client_secret_ref = fields.Char(
        size=128,
        groups="base.group_system",
        help="Environment key or mounted filename; never the OAuth client secret.",
    )
    refresh_token_ref = fields.Char(
        size=128,
        groups="base.group_system",
        help="Environment key or mounted filename; never the OAuth refresh token.",
    )
    developer_token_ref = fields.Char(
        required=True,
        size=128,
        groups="base.group_system",
        help="Environment key or mounted filename; never the developer token.",
    )
    service_account_json_ref = fields.Char(
        size=128,
        groups="base.group_system",
        help="Environment key or mounted filename; never the service-account JSON.",
    )
    login_customer_id = fields.Char(
        size=10,
        groups="base.group_system",
        help="Optional manager account ID, normalized without hyphens.",
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
            "The shared Google identity public reference must be unique.",
        ),
        (
            "company_name_unique",
            "unique(company_id, name)",
            "A Google identity with this name already exists for the company.",
        ),
        (
            "revision_positive",
            "check(revision > 0)",
            "The shared Google identity revision must be positive.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        normalized = []
        for incoming in vals_list:
            values = dict(incoming)
            if "public_ref" in values:
                raise AccessError(
                    _(
                        "The shared Google identity public reference is managed internally."
                    )
                )
            if "revision" in values:
                raise AccessError(
                    _("The shared Google identity revision is managed internally.")
                )
            self._normalize_configuration_values(values)
            normalized.append(values)
        return super().create(normalized)

    def write(self, values):
        values = dict(values)
        if "public_ref" in values:
            raise AccessError(
                _("The shared Google identity public reference is immutable.")
            )
        if "revision" in values:
            raise AccessError(
                _("The shared Google identity revision is managed internally.")
            )
        self._normalize_configuration_values(values)
        revision_fields = (
            "active",
            "api_version",
            "auth_mode",
            "credential_backend",
            "oauth_client_id_ref",
            "oauth_client_secret_ref",
            "refresh_token_ref",
            "developer_token_ref",
            "service_account_json_ref",
            "login_customer_id",
        )
        fields_to_lock = ["company_id", "revision", *revision_fields]
        self.flush_recordset(fields_to_lock)
        for identity in self.sorted("id"):
            identity.env.cr.execute(
                """
                SELECT company_id,
                       revision,
                       active,
                       api_version,
                       auth_mode,
                       credential_backend,
                       oauth_client_id_ref,
                       oauth_client_secret_ref,
                       refresh_token_ref,
                       developer_token_ref,
                       service_account_json_ref,
                       login_customer_id
                  FROM google_api_identity
                 WHERE id = %s
                   FOR UPDATE
                """,
                [identity.id],
            )
            locked = identity.env.cr.fetchone()
            if not locked:
                raise AccessError(_("The shared Google identity no longer exists."))
            company_id, current_revision, *configuration = locked
            if "company_id" in values and values["company_id"] != company_id:
                raise AccessError(
                    _("A shared Google identity cannot be moved to another company.")
                )
            persisted = dict(zip(revision_fields, configuration))
            identity_values = dict(values)
            if any(
                field_name in values and values[field_name] != persisted[field_name]
                for field_name in revision_fields
            ):
                identity_values["revision"] = current_revision + 1
            super(GoogleApiIdentity, identity).write(identity_values)
            identity.flush_recordset(list(identity_values))
            identity.invalidate_recordset(["revision"])
        return True

    @api.model
    def _normalize_configuration_values(self, values):
        for field_name in (
            "api_version",
            "oauth_client_id_ref",
            "oauth_client_secret_ref",
            "refresh_token_ref",
            "developer_token_ref",
            "service_account_json_ref",
        ):
            if field_name in values:
                values[field_name] = str(values[field_name] or "").strip()
        if "login_customer_id" in values:
            values["login_customer_id"] = _customer_id(values["login_customer_id"])

    @api.constrains(
        "api_version",
        "auth_mode",
        "credential_backend",
        "oauth_client_id_ref",
        "oauth_client_secret_ref",
        "refresh_token_ref",
        "developer_token_ref",
        "service_account_json_ref",
        "login_customer_id",
    )
    def _check_configuration(self):
        for identity in self:
            if not validate_api_version(identity.api_version):
                raise ValidationError(_("The Google Ads API version is unsupported."))
            if identity.login_customer_id and (
                not _CUSTOMER_ID_RE.fullmatch(identity.login_customer_id)
            ):
                raise ValidationError(
                    _("The Google Ads login customer ID must contain ten digits.")
                )
            invalid_reference = False
            try:
                reference_fields = ["developer_token_ref"]
                if identity.auth_mode == "authorized_user":
                    reference_fields.extend(
                        (
                            "oauth_client_id_ref",
                            "oauth_client_secret_ref",
                            "refresh_token_ref",
                        )
                    )
                elif identity.auth_mode == "service_account":
                    reference_fields.append("service_account_json_ref")
                else:
                    raise GoogleCredentialResolutionError(
                        "Google authentication mode is invalid"
                    )
                for field_name in reference_fields:
                    validate_credential_reference(
                        identity.credential_backend,
                        identity[field_name],
                    )
            except GoogleCredentialResolutionError:
                invalid_reference = True
            if invalid_reference:
                raise ValidationError(
                    _("A Google API credential reference is invalid.")
                )

    def _resolve_runtime(self, expected_revision=None):
        """Resolve one fenced, lock-free snapshot into process memory.

        Consumers must re-lock and revalidate the revision after provider I/O.
        Holding even ``FOR SHARE`` here would block credential rotation for the
        entire external request.
        """

        self.ensure_one()
        if (
            self.env.context.get(GOOGLE_API_RUNTIME_CONTEXT_KEY)
            is not GOOGLE_API_RUNTIME_TOKEN
        ):
            raise AccessError(_("Google API runtime is available only server-side."))
        self.check_access_rights("read")
        self.check_access_rule("read")
        if (
            expected_revision is None
            or isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision <= 0
        ):
            raise GoogleCredentialResolutionError(
                "Google identity expected revision is invalid"
            )
        runtime_fields = [
            "active",
            "api_version",
            "auth_mode",
            "company_id",
            "public_ref",
            "credential_backend",
            "oauth_client_id_ref",
            "oauth_client_secret_ref",
            "refresh_token_ref",
            "developer_token_ref",
            "service_account_json_ref",
            "login_customer_id",
            "revision",
        ]
        self.flush_recordset(runtime_fields)
        self.env.cr.execute(
            """
            SELECT active,
                   api_version,
                   auth_mode,
                   company_id,
                   public_ref,
                   credential_backend,
                   oauth_client_id_ref,
                   oauth_client_secret_ref,
                   refresh_token_ref,
                   developer_token_ref,
                   service_account_json_ref,
                   login_customer_id,
                   revision
              FROM google_api_identity
             WHERE id = %s
            """,
            [self.id],
        )
        snapshot = self.env.cr.fetchone()
        if not snapshot:
            raise GoogleCredentialResolutionError("Google identity is unavailable")
        (
            active,
            api_version,
            auth_mode,
            company_id,
            public_ref,
            credential_backend,
            oauth_client_id_ref,
            oauth_client_secret_ref,
            refresh_token_ref,
            developer_token_ref,
            service_account_json_ref,
            login_customer_id,
            revision,
        ) = snapshot
        if expected_revision != revision:
            raise GoogleCredentialResolutionError(
                "Google identity configuration changed"
            )
        if not active:
            raise GoogleCredentialResolutionError("Google identity is paused")
        runtime_values = {
            "active": active,
            "api_version": api_version,
            "auth_mode": auth_mode,
            "developer_token": resolve_credential(
                credential_backend, developer_token_ref
            ),
            "login_customer_id": login_customer_id or "",
            "public_ref": public_ref,
            "revision": revision,
            "company_id": company_id,
        }
        if auth_mode == "authorized_user":
            runtime_values.update(
                {
                    "oauth_client_id": resolve_credential(
                        credential_backend, oauth_client_id_ref
                    ),
                    "oauth_client_secret": resolve_credential(
                        credential_backend, oauth_client_secret_ref
                    ),
                    "refresh_token": resolve_credential(
                        credential_backend, refresh_token_ref
                    ),
                }
            )
        elif auth_mode == "service_account":
            runtime_values["service_account_info"] = resolve_service_account_info(
                credential_backend, service_account_json_ref
            )
        else:
            raise GoogleCredentialResolutionError(
                "Google authentication mode is invalid"
            )
        return GoogleRuntimeIdentity(**runtime_values)

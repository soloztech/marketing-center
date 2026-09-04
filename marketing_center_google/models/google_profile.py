import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.tokens import (
    MARKETING_GOOGLE_CONNECTION_TOKEN,
    MARKETING_GOOGLE_PROFILE_RUNTIME_TOKEN,
)


def _uuid(_recordset):
    return str(uuid.uuid4())


class MarketingCenterGoogleProfile(models.Model):
    _name = "marketing.center.google.profile"
    _description = "Marketing Center Google Ads Reader Profile"
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
    google_identity_id = fields.Many2one(
        "google.api.identity",
        string="Google API Identity",
        required=True,
        index=True,
        check_company=True,
        ondelete="restrict",
    )
    identity_revision = fields.Integer(required=True, readonly=True, copy=False)
    profile_revision = fields.Integer(
        required=True,
        default=1,
        readonly=True,
        copy=False,
    )
    max_discovery_roots = fields.Integer(required=True, default=10)
    max_discovery_depth = fields.Integer(required=True, default=3)
    max_discovery_customers = fields.Integer(required=True, default=200)
    max_discovery_pages = fields.Integer(required=True, default=2)
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
    last_request_id = fields.Char(readonly=True, copy=False, size=128)
    accessible_root_count = fields.Integer(readonly=True, copy=False)
    discovered_customer_count = fields.Integer(readonly=True, copy=False)
    discovery_request_count = fields.Integer(readonly=True, copy=False)
    connection_ids = fields.One2many(
        "marketing.center.connection",
        "google_profile_id",
        readonly=True,
    )

    _sql_constraints = [
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The Google profile public reference must be unique.",
        ),
        (
            "revision_positive",
            "check(profile_revision > 0 and identity_revision > 0)",
            "Google profile revisions must be positive.",
        ),
        (
            "discovery_bounds",
            "check(max_discovery_roots between 1 and 50 "
            "and max_discovery_depth between 1 and 5 "
            "and max_discovery_customers between 1 and 500 "
            "and max_discovery_pages between 1 and 4)",
            "Google discovery limits are invalid.",
        ),
    ]

    def init(self):
        self.env.cr.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS mc_google_profile_identity_active_uq "
            "ON marketing_center_google_profile (google_identity_id) "
            "WHERE active IS TRUE"
        )

    @api.model_create_multi
    def create(self, vals_list):
        normalized = []
        for incoming in vals_list:
            values = dict(incoming)
            if "public_ref" in values or "profile_revision" in values:
                raise AccessError(_("Google profile identity is managed internally."))
            identity = (
                self.env["google.api.identity"]
                .browse(values.get("google_identity_id"))
                .exists()
            )
            if not identity or len(identity) != 1:
                raise ValidationError(_("A Google API identity is required."))
            values["identity_revision"] = identity.revision
            if (
                self._runtime_fields().intersection(values)
                and not self._runtime_internal()
            ):
                raise AccessError(_("Google profile health is maintained internally."))
            normalized.append(values)
        return super().create(normalized)

    def write(self, values):
        values = dict(values)
        if "public_ref" in values:
            raise AccessError(_("The Google profile public reference is immutable."))
        if "company_id" in values and any(
            profile.company_id.id != values["company_id"] for profile in self
        ):
            raise AccessError(_("A Google profile cannot move to another company."))
        if "profile_revision" in values:
            raise AccessError(_("The Google profile revision is managed internally."))
        if "identity_revision" in values and not self._runtime_internal():
            raise AccessError(_("The Google identity revision is managed internally."))
        if self._runtime_fields().intersection(values) and not self._runtime_internal():
            raise AccessError(_("Google profile health is maintained internally."))
        revision_fields = (
            "active",
            "google_identity_id",
            "identity_revision",
            "max_discovery_roots",
            "max_discovery_depth",
            "max_discovery_customers",
            "max_discovery_pages",
        )
        if not set(revision_fields).intersection(values):
            return super().write(values)
        if "google_identity_id" in values:
            identity = (
                self.env["google.api.identity"]
                .browse(values["google_identity_id"])
                .exists()
            )
            if not identity or len(identity) != 1:
                raise ValidationError(_("A Google API identity is required."))
            values["identity_revision"] = identity.revision
        self.flush_recordset(["company_id", "profile_revision", *revision_fields])
        for profile in self.sorted("id"):
            self.env.cr.execute(
                "SELECT company_id, profile_revision, active, google_identity_id, "
                "identity_revision, max_discovery_roots, max_discovery_depth, "
                "max_discovery_customers, max_discovery_pages "
                "FROM marketing_center_google_profile "
                "WHERE id = %s FOR UPDATE",
                [profile.id],
            )
            locked = self.env.cr.fetchone()
            if not locked:
                raise AccessError(_("The Google profile no longer exists."))
            company_id, current_revision, *configuration = locked
            if "company_id" in values and values["company_id"] != company_id:
                raise AccessError(_("A Google profile cannot move to another company."))
            profile_values = dict(values)
            persisted = dict(zip(revision_fields, configuration))
            configuration_changed = any(
                field_name in profile_values
                and profile_values[field_name] != persisted[field_name]
                for field_name in revision_fields
            )
            if configuration_changed:
                profile_values.update(self._cleared_health())
                profile_values["profile_revision"] = current_revision + 1
            super(MarketingCenterGoogleProfile, profile).write(profile_values)
            profile.flush_recordset(list(profile_values))
            profile.invalidate_recordset(["profile_revision", "identity_revision"])
            if configuration_changed:
                profile._pause_bound_connections()
        return True

    def _adopt_current_identity_revision(self):
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM google_api_identity WHERE id = %s FOR UPDATE",
            [self.google_identity_id.id],
        )
        self.google_identity_id.invalidate_recordset(["active", "revision"])
        self.invalidate_recordset(["identity_revision", "profile_revision"])
        if not self.google_identity_id.active:
            raise ValidationError(_("The Google API identity is paused."))
        if self.identity_revision == self.google_identity_id.revision:
            return self
        self.with_context(
            marketing_google_profile_runtime_token=MARKETING_GOOGLE_PROFILE_RUNTIME_TOKEN
        ).write({"identity_revision": self.google_identity_id.revision})
        return self

    def action_enqueue_validation(self):
        self.ensure_one()
        self.check_access_rights("write")
        self.check_access_rule("write")
        self._adopt_current_identity_revision()
        self.with_delay(
            identity_key="marketing_google:validate:%s:%s:%s"
            % (self.public_ref, self.profile_revision, self.identity_revision),
            max_retries=8,
            priority=20,
            description="Validate Google Ads reader %s" % self.public_ref,
        )._job_validate_google_profile(
            expected_profile_revision=self.profile_revision,
            expected_identity_revision=self.identity_revision,
        )
        return True

    def action_enqueue_discovery(self):
        self.ensure_one()
        self.check_access_rights("write")
        self.check_access_rule("write")
        self._adopt_current_identity_revision()
        self.with_delay(
            identity_key="marketing_google:discover:%s:%s:%s"
            % (self.public_ref, self.profile_revision, self.identity_revision),
            max_retries=8,
            priority=30,
            description="Discover Google Ads customers %s" % self.public_ref,
        )._job_discover_google_sources(
            expected_profile_revision=self.profile_revision,
            expected_identity_revision=self.identity_revision,
        )
        return True

    def _job_validate_google_profile(
        self, expected_profile_revision, expected_identity_revision
    ):
        self.ensure_one()
        return self.env["marketing.center.google.service"]._validate_profile(
            self,
            expected_profile_revision=expected_profile_revision,
            expected_identity_revision=expected_identity_revision,
        )

    def _job_discover_google_sources(
        self, expected_profile_revision, expected_identity_revision
    ):
        self.ensure_one()
        return self.env["marketing.center.google.service"]._discover_sources(
            self,
            expected_profile_revision=expected_profile_revision,
            expected_identity_revision=expected_identity_revision,
        )

    def _queue_job_attempt_is_terminal(self, expected_method):
        """Return whether the exact running OCA job exhausted its retry budget."""

        self.ensure_one()
        allowed_methods = {
            "_job_validate_google_profile",
            "_job_discover_google_sources",
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

    @api.model
    def _runtime_fields(self):
        return {
            "health_state",
            "verified_at",
            "last_error_class",
            "last_error_message",
            "last_request_id",
            "accessible_root_count",
            "discovered_customer_count",
            "discovery_request_count",
        }

    def _runtime_internal(self):
        return (
            self.env.context.get("marketing_google_profile_runtime_token")
            is MARKETING_GOOGLE_PROFILE_RUNTIME_TOKEN
        )

    @api.model
    def _cleared_health(self):
        return {
            "health_state": "unknown",
            "verified_at": False,
            "last_error_class": False,
            "last_error_message": False,
            "last_request_id": False,
            "accessible_root_count": 0,
            "discovered_customer_count": 0,
            "discovery_request_count": 0,
        }

    def _pause_bound_connections(self):
        connections = self.connection_ids.filtered(
            lambda connection: connection.active
            and connection.state not in {"paused", "disabled"}
        )
        if connections:
            connections.with_context(
                marketing_google_connection_token=MARKETING_GOOGLE_CONNECTION_TOKEN,
                marketing_configuration_runtime_token=self.env[
                    "marketing.center.google.service"
                ]._configuration_runtime_token(),
            ).write(
                {
                    "state": "paused",
                    "health_state": "degraded",
                    "last_health_error_class": "profile_changed",
                    "last_health_error_message": (
                        "Google reader configuration changed; validate it again."
                    ),
                }
            )

    @api.constrains("company_id", "google_identity_id")
    def _check_company_identity(self):
        for profile in self:
            if profile.google_identity_id.company_id != profile.company_id:
                raise ValidationError(
                    _("The Google identity and profile must share one company.")
                )

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Google profiles must be archived instead of deleted."))

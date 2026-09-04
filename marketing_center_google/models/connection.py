import re
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.catalog import GOOGLE_ADAPTER_KEY, GOOGLE_ADS_SERVICE
from ..services.tokens import MARKETING_GOOGLE_CONNECTION_TOKEN

_GOOGLE_CUSTOMER_ID_RE = re.compile(r"^[0-9]{10}$")


class MarketingCenterConnection(models.Model):
    _inherit = "marketing.center.connection"

    _sql_constraints = [
        (
            "google_reader_revision_positive",
            "check(adapter_key != 'google.ads.rest.v25' "
            "or (google_profile_id is not null and google_identity_revision > 0 "
            "and (google_login_customer_id is null "
            "or google_login_customer_id ~ '^[0-9]{10}$')))",
            "Google reader identity revisions must be positive.",
        )
    ]

    google_profile_id = fields.Many2one(
        "marketing.center.google.profile",
        index=True,
        ondelete="restrict",
        check_company=True,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    google_identity_revision = fields.Integer(
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    google_login_customer_id = fields.Char(
        string="Effective Google Login Customer",
        size=10,
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_admin",
        help=(
            "Revision-fenced Google Ads manager customer used for this source. "
            "It is empty only for a directly accessible non-manager customer."
        ),
    )

    @api.model_create_multi
    def create(self, vals_list):
        self._lock_source_identity_scope(vals_list)
        return super().create(
            [self._prepare_google(values, False) for values in vals_list]
        )

    def write(self, values):
        values = dict(values)
        if len(self) > 1 and self._google_binding_fields().intersection(values):
            for connection in self:
                connection.write(values)
            return True
        if len(self) == 1:
            values = self._prepare_google(values, self)
        return super().write(values)

    @api.model
    def _google_binding_fields(self):
        return {
            "adapter_key",
            "google_profile_id",
            "google_identity_revision",
            "google_login_customer_id",
            "profile_public_ref",
            "profile_revision",
            "purpose",
            "source_id",
            "state",
        }

    @api.model
    def _prepare_google(self, incoming, record):
        values = dict(incoming)
        current_adapter = record.adapter_key if record else ""
        internal = self._google_internal()
        adapter_key = (
            str(values.get("adapter_key", current_adapter) or "").strip().lower()
        )
        if current_adapter == GOOGLE_ADAPTER_KEY and adapter_key != GOOGLE_ADAPTER_KEY:
            raise AccessError(_("A Google connection adapter is immutable."))
        profile_id = values.get(
            "google_profile_id", record.google_profile_id.id if record else False
        )
        profile = (
            self.env["marketing.center.google.profile"].browse(profile_id).exists()
        )
        if adapter_key != GOOGLE_ADAPTER_KEY:
            if (
                profile
                or values.get("google_identity_revision")
                or values.get("google_login_customer_id")
            ):
                raise ValidationError(
                    _("A Google profile requires the Google adapter.")
                )
            return values
        if not profile or len(profile) != 1:
            raise ValidationError(_("A Google reader profile is required."))
        source_id = values.get("source_id", record.source_id.id if record else False)
        source = self.env["marketing.center.source"].browse(source_id).exists()
        if (
            not source
            or len(source) != 1
            or source.service != GOOGLE_ADS_SERVICE
            or source.company_id != profile.company_id
        ):
            raise ValidationError(_("A Google reader requires a Google Ads source."))
        if values.get("purpose", record.purpose if record else "reader") != "reader":
            raise ValidationError(_("This release supports only Google reads."))
        if (
            record
            and "google_profile_id" in values
            and profile != record.google_profile_id
            and not internal
        ):
            raise AccessError(_("The Google profile binding is maintained internally."))
        if values.get("state") == "ready" and not internal:
            raise AccessError(_("A Google connection becomes ready after discovery."))
        protected = {
            "profile_public_ref",
            "profile_revision",
            "google_identity_revision",
            "google_login_customer_id",
        }.intersection(values)
        if protected and not internal:
            raise AccessError(_("The Google profile binding is maintained internally."))
        desired_binding = {
            "profile_public_ref": profile.public_ref,
            "profile_revision": profile.profile_revision,
            "google_identity_revision": profile.identity_revision,
        }
        if not record or "google_profile_id" in values:
            values.update(desired_binding)
        elif internal:
            for field_name, value in desired_binding.items():
                if record[field_name] != value:
                    values[field_name] = value
        self._prepare_google_login(values, record, profile)
        return values

    @api.model
    def _prepare_google_login(self, values, record, profile):
        if "google_login_customer_id" not in values:
            return
        login_customer_id = str(values.get("google_login_customer_id") or "").strip()
        if login_customer_id and not _GOOGLE_CUSTOMER_ID_RE.fullmatch(
            login_customer_id
        ):
            raise ValidationError(_("The Google login customer is invalid."))
        values["google_login_customer_id"] = login_customer_id or False
        current_login = record.google_login_customer_id if record else False
        if record and (current_login or False) != (
            values["google_login_customer_id"] or False
        ):
            # The neutral base increments its binding fence when this key is
            # present. Reusing the current profile value makes the new header
            # scope part of the immutable synchronization snapshot.
            values["profile_revision"] = profile.profile_revision

    def _google_internal(self):
        return (
            self.env.context.get("marketing_google_connection_token")
            is MARKETING_GOOGLE_CONNECTION_TOKEN
        )

    @api.constrains(
        "active",
        "adapter_key",
        "google_profile_id",
        "google_identity_revision",
        "google_login_customer_id",
        "purpose",
        "source_id",
        "profile_public_ref",
        "profile_revision",
        "state",
    )
    def _check_google_binding(self):
        for connection in self:
            if connection.adapter_key != GOOGLE_ADAPTER_KEY:
                if connection.google_profile_id:
                    raise ValidationError(_("A Google profile requires its adapter."))
                continue
            profile = connection.google_profile_id
            identity_login = (
                str(profile.google_identity_id.login_customer_id or "").strip()
                if profile
                else ""
            )
            if (
                not profile
                or connection.purpose != "reader"
                or connection.source_id.service != GOOGLE_ADS_SERVICE
                or profile.company_id != connection.company_id
                or profile.public_ref != connection.profile_public_ref
                or profile.profile_revision < connection.profile_revision
                or profile.identity_revision < connection.google_identity_revision
                or (
                    identity_login
                    and identity_login != connection.google_login_customer_id
                )
                or (
                    connection.state == "ready"
                    and (
                        not profile.active
                        or profile.profile_revision != connection.profile_revision
                        or profile.identity_revision
                        != connection.google_identity_revision
                        or profile.google_identity_id.revision
                        != connection.google_identity_revision
                    )
                )
            ):
                raise ValidationError(_("The Google reader binding is inconsistent."))


class MarketingCenterSource(models.Model):
    _inherit = "marketing.center.source"

    @api.model
    def _identity_evidence_registry(self):
        return super()._identity_evidence_registry() + (
            ("marketing.center.google.change.observation", "source_id", ()),
            ("marketing.center.google.diagnostic.observation", "source_id", ()),
        )

    def action_enqueue_google_catalog_sync(self):
        self._check_google_manual_sync_access()
        service = self.env["marketing.center.google.catalog.service"]
        connection = service._reader_connection(self)
        run = service._plan_sweep(
            self,
            connection,
            trigger_kind="manual",
            trigger_ref="manual:%s" % uuid.uuid4(),
        )
        cursor_sequence = service._restart_cursor(run)
        service._enqueue_catalog_page(run, cursor_sequence)
        return True

    def action_enqueue_google_performance_sync(self):
        self._check_google_manual_sync_access()
        self.env["marketing.center.google.performance.service"]._enqueue_manual(
            self, lookback_days=7
        )
        return True

    def action_enqueue_google_change_sync(self):
        self._check_google_manual_sync_access()
        self.env["marketing.center.google.change.service"]._enqueue_manual(
            self, lookback_days=7
        )
        return True

    def action_enqueue_google_diagnostic_sync(self):
        self._check_google_manual_sync_access()
        self.env["marketing.center.google.diagnostic.service"]._enqueue_manual(self)
        return True

    def _check_google_manual_sync_access(self):
        self.ensure_one()
        self.check_access_rights("write")
        self.check_access_rule("write")
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_admin"
        ):
            raise AccessError(_("Only administrators can synchronize Google Ads."))

    @api.constrains("service", "external_account_ref", "external_account_id")
    def _check_google_customer_identity(self):
        for source in self:
            if source.service != GOOGLE_ADS_SERVICE:
                continue
            expected_ref = "customers/%s" % (source.external_account_id or "")
            if (
                not _GOOGLE_CUSTOMER_ID_RE.fullmatch(source.external_account_id or "")
                or source.external_account_ref != expected_ref
            ):
                raise ValidationError(
                    _("The Google Ads customer identity is inconsistent.")
                )

import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.adapter import META_ADAPTER_KEY, META_ADS_SERVICE
from ..services.tokens import MARKETING_META_CONNECTION_TOKEN


class MarketingCenterConnection(models.Model):
    _inherit = "marketing.center.connection"

    meta_profile_id = fields.Many2one(
        "marketing.center.meta.profile",
        index=True,
        ondelete="restrict",
        check_company=True,
        groups="marketing_center_base.group_marketing_center_admin",
    )

    @api.model_create_multi
    def create(self, vals_list):
        prepared = [self._prepare_meta_create(values) for values in vals_list]
        return super().create(prepared)

    def write(self, values):
        values = dict(values)
        if not self:
            return super().write(values)
        if len(self) > 1:
            if self._meta_binding_fields().intersection(values):
                for connection in self:
                    connection.write(values)
                return True
            return super().write(values)
        values = self._prepare_meta_write(values)
        return super().write(values)

    @api.model
    def _meta_binding_fields(self):
        return {
            "adapter_key",
            "meta_profile_id",
            "profile_public_ref",
            "profile_revision",
            "purpose",
            "source_id",
            "state",
        }

    @api.model
    def _prepare_meta_create(self, incoming):
        values = dict(incoming)
        adapter_key = str(values.get("adapter_key") or "").strip().lower()
        profile = self.env["marketing.center.meta.profile"].browse(
            values.get("meta_profile_id")
        )
        if adapter_key != META_ADAPTER_KEY:
            if profile:
                raise ValidationError(_("A Meta profile requires the Meta adapter."))
            return values
        profile = profile.exists()
        if not profile or len(profile) != 1:
            raise ValidationError(_("A Meta reader profile is required."))
        source = (
            self.env["marketing.center.source"].browse(values.get("source_id")).exists()
        )
        if (
            not source
            or len(source) != 1
            or source.service != META_ADS_SERVICE
            or source.company_id != profile.company_id
        ):
            raise ValidationError(_("A Meta reader requires a Meta Ads source."))
        if values.get("purpose", "reader") != "reader":
            raise ValidationError(
                _("This release supports only Meta read connections.")
            )
        if values.get("state") == "ready" and not self._meta_internal():
            raise AccessError(
                _("A Meta connection becomes ready only after validation.")
            )
        values.update(
            {
                "profile_public_ref": profile.public_ref,
                "profile_revision": profile.profile_revision,
            }
        )
        return values

    def _prepare_meta_write(self, incoming):
        self.ensure_one()
        values = dict(incoming)
        adapter_key = (
            str(values.get("adapter_key", self.adapter_key) or "").strip().lower()
        )
        if self.adapter_key == META_ADAPTER_KEY and adapter_key != META_ADAPTER_KEY:
            raise AccessError(
                _("A Meta connection adapter cannot be changed; archive it instead.")
            )
        profile_id = values.get("meta_profile_id", self.meta_profile_id.id)
        profile = self.env["marketing.center.meta.profile"].browse(profile_id).exists()
        if adapter_key != META_ADAPTER_KEY:
            if profile:
                raise ValidationError(_("A Meta profile requires the Meta adapter."))
            return values
        if not profile or len(profile) != 1:
            raise ValidationError(_("A Meta reader profile is required."))
        source = (
            self.env["marketing.center.source"]
            .browse(values.get("source_id", self.source_id.id))
            .exists()
        )
        if (
            not source
            or len(source) != 1
            or source.service != META_ADS_SERVICE
            or source.company_id != profile.company_id
        ):
            raise ValidationError(_("A Meta reader requires a Meta Ads source."))
        purpose = values.get("purpose", self.purpose)
        if purpose != "reader":
            raise ValidationError(
                _("This release supports only Meta read connections.")
            )
        if values.get("state") == "ready" and not self._meta_internal():
            raise AccessError(
                _("A Meta connection becomes ready only after validation.")
            )
        protected = {"profile_public_ref", "profile_revision"}.intersection(values)
        if protected and not self._meta_internal():
            raise AccessError(_("The Meta profile binding is maintained internally."))
        if "meta_profile_id" in values:
            if not self._meta_internal() and self.state == "ready":
                raise AccessError(
                    _("Pause the Meta connection before changing profile.")
                )
            values.update(
                {
                    "profile_public_ref": profile.public_ref,
                    "profile_revision": profile.profile_revision,
                }
            )
        return values

    def _meta_internal(self):
        return (
            self.env.context.get("marketing_meta_connection_token")
            is MARKETING_META_CONNECTION_TOKEN
        )

    @api.constrains(
        "active",
        "adapter_key",
        "meta_profile_id",
        "purpose",
        "source_id",
        "profile_public_ref",
        "profile_revision",
        "state",
    )
    def _check_meta_binding(self):
        for connection in self:
            if connection.adapter_key != META_ADAPTER_KEY:
                if connection.meta_profile_id:
                    raise ValidationError(
                        _("A Meta profile requires the Meta adapter.")
                    )
                continue
            profile = connection.meta_profile_id
            if (
                not profile
                or connection.purpose != "reader"
                or connection.source_id.service != META_ADS_SERVICE
                or profile.company_id != connection.company_id
                or profile.public_ref != connection.profile_public_ref
                or profile.profile_revision < connection.profile_revision
                or (
                    connection.state == "ready"
                    and (
                        not profile.active
                        or profile.profile_revision != connection.profile_revision
                    )
                )
            ):
                raise ValidationError(_("The Meta reader binding is inconsistent."))


class MarketingCenterSource(models.Model):
    _inherit = "marketing.center.source"

    def action_enqueue_meta_catalog_sync(self):
        self.ensure_one()
        self.check_access_rights("write")
        self.check_access_rule("write")
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_admin"
        ):
            raise AccessError(_("Only Marketing Center administrators can sync Meta."))
        service = self.env["marketing.center.meta.catalog.service"]
        connection = service._reader_connection(self)
        run = service._plan_sweep(
            self,
            connection,
            trigger_kind="manual",
            trigger_ref="manual:%s" % uuid.uuid4(),
        )
        service._enqueue_page(run, service._cursor_snapshot(run)[0])
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Meta catalog"),
                "message": _("The read-only Meta catalog sync was queued."),
                "type": "info",
                "sticky": False,
            },
        }

    def write(self, values):
        identity_fields = {"service", "external_account_ref"}.intersection(values)
        if identity_fields:
            service = str(values.get("service", "") or "").strip().lower()
            external_ref = str(values.get("external_account_ref", "") or "").strip()
            changed = self.filtered(
                lambda source: (
                    "service" in identity_fields and source.service != service
                )
                or (
                    "external_account_ref" in identity_fields
                    and source.external_account_ref != external_ref
                )
            )
            if changed and self.env["marketing.center.connection"].sudo().search_count(
                [
                    ("source_id", "in", changed.ids),
                    ("adapter_key", "=", META_ADAPTER_KEY),
                ]
            ):
                raise AccessError(
                    _("A source bound to Meta cannot change its external identity.")
                )
        return super().write(values)

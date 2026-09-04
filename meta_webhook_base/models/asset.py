import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

_ID_RE = re.compile(r"^[0-9]{1,40}$")
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class MetaWebhookAsset(models.Model):
    _name = "meta.webhook.asset"
    _description = "Meta Webhook Routing Asset"
    _order = "endpoint_id, platform, external_asset_id, id"
    _check_company_auto = True

    active = fields.Boolean(default=True, index=True)
    page_id = fields.Many2one(
        "meta.webhook.page",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    endpoint_id = fields.Many2one(
        related="page_id.endpoint_id", store=True, readonly=True, index=True
    )
    company_id = fields.Many2one(
        related="page_id.company_id", store=True, readonly=True, index=True
    )
    platform = fields.Selection(
        [("facebook", "Facebook"), ("instagram", "Instagram")],
        required=True,
        index=True,
    )
    object_type = fields.Char(required=True, size=64, index=True)
    transport = fields.Char(required=True, size=64, index=True)
    external_asset_id = fields.Char(required=True, size=40, index=True, copy=False)

    _sql_constraints = [
        (
            "endpoint_asset_unique",
            "unique(endpoint_id, object_type, external_asset_id)",
            "This Meta asset is already routed on the webhook endpoint.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        normalized = [self._normalized_values(values) for values in vals_list]
        return super().create(normalized)

    def write(self, values):
        if not self:
            return True
        values = self._normalized_values(values)
        immutable = {
            "page_id",
            "external_asset_id",
            "platform",
            "object_type",
            "transport",
        }.intersection(values)
        if immutable:
            raise AccessError(_("Meta webhook asset identity is immutable."))
        if "active" in values:
            pages = self.mapped("page_id")
            endpoints = pages.mapped("endpoint_id")
            # Fan-out/dispatch evaluates policy under Endpoint -> App -> Page ->
            # Asset shared locks. Archive in the compatible Endpoint -> Page ->
            # Asset order so a pending dispatch is linearized before or after
            # retirement instead of racing an unlocked boolean read.
            endpoints.flush_recordset(["revision"])
            if endpoints:
                self.env.cr.execute(
                    "SELECT id FROM meta_webhook_endpoint WHERE id IN %s "
                    "ORDER BY id FOR UPDATE",
                    [tuple(endpoints.ids)],
                )
            pages.flush_recordset(["revision"])
            if pages:
                self.env.cr.execute(
                    "SELECT id FROM meta_webhook_page WHERE id IN %s "
                    "ORDER BY id FOR UPDATE",
                    [tuple(pages.ids)],
                )
            self.flush_recordset(["active"])
            self.env.cr.execute(
                "SELECT id FROM meta_webhook_asset WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                [tuple(self.ids)],
            )
        return super().write(values)

    @api.model
    def _normalized_values(self, incoming):
        values = dict(incoming)
        for field_name in ("object_type", "transport"):
            if field_name in values:
                values[field_name] = str(values[field_name] or "").strip().lower()
        if "external_asset_id" in values:
            values["external_asset_id"] = str(values["external_asset_id"] or "").strip()
        return values

    @api.constrains("external_asset_id", "object_type", "transport")
    def _check_values(self):
        for asset in self:
            if not _ID_RE.fullmatch(asset.external_asset_id or ""):
                raise ValidationError(_("The external Meta asset ID is invalid."))
            if not _TOKEN_RE.fullmatch(asset.object_type or ""):
                raise ValidationError(_("The Meta asset object type is invalid."))
            if not _TOKEN_RE.fullmatch(asset.transport or ""):
                raise ValidationError(_("The Meta asset transport is invalid."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Meta webhook assets must be archived."))

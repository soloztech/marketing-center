import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SUPPORTED_OBJECT_TYPES = {"page", "instagram"}


class MetaWebhookSubscription(models.Model):
    _name = "meta.webhook.subscription"
    _description = "Meta Webhook Consumer Subscription"
    _order = "page_id, object_type, field_name, consumer_key, id"
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
    consumer_key = fields.Char(required=True, size=128, index=True)
    object_type = fields.Char(required=True, size=64, index=True, default="page")
    field_name = fields.Char(required=True, size=64, index=True)

    _sql_constraints = [
        (
            "consumer_field_unique",
            "unique(page_id, consumer_key, object_type, field_name)",
            "This Meta webhook consumer field is already registered.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        normalized = [self._normalized_values(values) for values in vals_list]
        page_ids = sorted(
            {
                values.get("page_id")
                for values in normalized
                if isinstance(values.get("page_id"), int)
                and not isinstance(values.get("page_id"), bool)
            }
        )
        pages = self.env["meta.webhook.page"].browse(page_ids).exists()
        endpoints = pages.mapped("endpoint_id")
        # The FK insert takes a KEY SHARE lock on Page.  Lock Endpoint first so
        # CREATE follows the same Endpoint -> Page order as reconciliation and
        # cannot deadlock with its Page projection.
        endpoints.flush_recordset(["revision"])
        if endpoints:
            self.env.cr.execute(
                "SELECT id FROM meta_webhook_endpoint WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                [tuple(endpoints.ids)],
            )
        subscriptions = super().create(normalized)
        subscriptions.mapped("page_id")._meta_subscription_configuration_changed()
        return subscriptions

    def write(self, values):
        if not self:
            return True
        values = self._normalized_values(values)
        if "page_id" in values and any(
            subscription.page_id.id != values["page_id"] for subscription in self
        ):
            raise AccessError(_("A Meta subscription cannot change Page."))
        structural = (
            "active",
            "consumer_key",
            "object_type",
            "field_name",
        )
        requested_structural = set(structural).intersection(values)
        if not requested_structural:
            return super().write(values)

        pages = self.mapped("page_id")
        endpoints = pages.mapped("endpoint_id")
        # Use the same Endpoint -> Page -> Subscription order as the union
        # projection.  Besides avoiding lock inversion, the final row snapshot
        # makes change detection independent from a potentially stale ORM cache.
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
        self.flush_recordset(list(structural))
        self.env.cr.execute(
            "SELECT id, active, consumer_key, object_type, field_name "
            "FROM meta_webhook_subscription WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            [tuple(self.ids)],
        )
        persisted = {
            row[0]: dict(zip(structural, row[1:])) for row in self.env.cr.fetchall()
        }
        changed = self.filtered(
            lambda subscription: any(
                values[field_name] != persisted.get(subscription.id, {}).get(field_name)
                for field_name in requested_structural
            )
        )
        if not changed and set(values).issubset(requested_structural):
            return True
        # The row snapshot above is authoritative.  A concurrent writer (or a
        # direct SQL maintenance operation) may have changed a structural value
        # while this Environment still holds the former value in its cache.
        # Drop those clean cached values before delegating to the ORM so write()
        # cannot incorrectly optimize an actual database transition away.
        self.invalidate_recordset(list(requested_structural))
        result = super().write(values)
        if changed:
            changed.mapped("page_id")._meta_subscription_configuration_changed()
        return result

    @api.model
    def _normalized_values(self, incoming):
        values = dict(incoming)
        for field_name in ("consumer_key", "object_type", "field_name"):
            if field_name in values:
                values[field_name] = str(values[field_name] or "").strip().lower()
        return values

    @api.constrains("consumer_key", "object_type", "field_name")
    def _check_keys(self):
        for subscription in self:
            if not _KEY_RE.fullmatch(subscription.consumer_key or ""):
                raise ValidationError(_("The Meta webhook consumer key is invalid."))
            if not _FIELD_RE.fullmatch(subscription.object_type or ""):
                raise ValidationError(_("The Meta webhook object type is invalid."))
            if subscription.object_type not in _SUPPORTED_OBJECT_TYPES:
                raise ValidationError(_("The Meta webhook object type is unsupported."))
            if not _FIELD_RE.fullmatch(subscription.field_name or ""):
                raise ValidationError(_("The Meta webhook field name is invalid."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Meta webhook subscriptions must be archived."))

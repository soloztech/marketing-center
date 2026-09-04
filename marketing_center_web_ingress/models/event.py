import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from ..services.tokens import WEB_INGRESS_INTERNAL_TOKEN

INGRESS_PROVENANCE_SELECTION = [
    ("browser_capability", "Browser capability"),
    ("server_internal", "Internal server adapter"),
    ("unclassified", "Unknown provenance (preserved evidence)"),
    ("website_confirmed_action", "Confirmed Odoo Website action"),
]


def _uuid(_recordset):
    return str(uuid.uuid4())


class MarketingWebIngressEvent(models.Model):
    _name = "marketing.web.ingress.event"
    _description = "Sanitized Marketing Web Ingress Event"
    _order = "observed_at desc, id desc"
    _rec_name = "public_ref"
    _check_company_auto = True

    endpoint_id = fields.Many2one(
        "marketing.web.ingress.endpoint",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    company_id = fields.Many2one(
        related="endpoint_id.company_id", store=True, readonly=True, index=True
    )
    public_ref = fields.Char(
        required=True,
        default=_uuid,
        size=36,
        readonly=True,
        copy=False,
        index=True,
    )
    event_key_hash = fields.Char(required=True, size=64, index=True, readonly=True)
    request_digest = fields.Char(required=True, size=64, index=True, readonly=True)
    origin = fields.Char(required=True, readonly=True)
    landing_host = fields.Char(required=True, index=True, readonly=True)
    occurred_at = fields.Datetime(required=True, index=True, readonly=True)
    observed_at = fields.Datetime(required=True, index=True, readonly=True)
    body_size_bytes = fields.Integer(required=True, readonly=True)
    key_revision = fields.Integer(required=True, readonly=True)
    config_revision = fields.Integer(required=True, readonly=True)
    ingress_provenance = fields.Selection(
        INGRESS_PROVENANCE_SELECTION,
        required=True,
        index=True,
        readonly=True,
    )
    state = fields.Selection(
        [("processing", "Processing"), ("done", "Done")],
        required=True,
        default="processing",
        index=True,
        readonly=True,
    )
    touchpoint_id = fields.Many2one(
        "marketing.attribution.touchpoint",
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    click_value_ids = fields.One2many(
        "marketing.web.ingress.click.value", "event_id", readonly=True
    )

    _sql_constraints = [
        (
            "endpoint_event_key_unique",
            "unique(endpoint_id, event_key_hash)",
            "This client event was already accepted by the endpoint.",
        ),
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The web ingress event public reference must be unique.",
        ),
        (
            "event_hashes_sha256",
            "check(char_length(event_key_hash) = 64 and char_length(request_digest) = 64)",
            "Web ingress event hashes must be SHA-256 digests.",
        ),
        (
            "event_runtime_positive",
            "check(body_size_bytes >= 0 and key_revision > 0 and config_revision > 0)",
            "Web ingress runtime evidence is invalid.",
        ),
        (
            "event_projection_consistent",
            "check((state = 'processing' and touchpoint_id is null) or "
            "(state = 'done' and touchpoint_id is not null))",
            "Web ingress event projection state is inconsistent.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("marketing_web_ingress_internal")
            is not WEB_INGRESS_INTERNAL_TOKEN
        ):
            raise AccessError(
                _("Web ingress events are created only by their service.")
            )
        if any(
            values.get("ingress_provenance")
            not in {
                "browser_capability",
                "server_internal",
                "website_confirmed_action",
            }
            for values in vals_list
        ):
            raise AccessError(
                _("New web ingress events require classified provenance.")
            )
        if any(
            values.get("state", "processing") != "processing"
            or values.get("touchpoint_id")
            for values in vals_list
        ):
            raise AccessError(
                _("New web ingress events must begin in the processing state.")
            )
        return super().create(vals_list)

    def write(self, values):
        if (
            self.env.context.get("marketing_web_ingress_internal")
            is not WEB_INGRESS_INTERNAL_TOKEN
        ):
            raise AccessError(_("Web ingress events are immutable."))
        if (
            set(values) != {"state", "touchpoint_id"}
            or values.get("state") != "done"
            or not values.get("touchpoint_id")
        ):
            raise AccessError(_("Web ingress event evidence is immutable."))
        if any(event.state != "processing" for event in self):
            raise AccessError(_("A finalized web ingress event is immutable."))
        return super().write(values)

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Web ingress events cannot be deleted."))


class MarketingWebIngressClickValue(models.Model):
    _name = "marketing.web.ingress.click.value"
    _description = "Protected Marketing Click Identifier"
    _order = "event_id, namespace, id"
    _rec_name = "value_ref"
    _check_company_auto = True

    event_id = fields.Many2one(
        "marketing.web.ingress.event",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        readonly=True,
    )
    company_id = fields.Many2one(
        related="event_id.company_id", store=True, readonly=True, index=True
    )
    namespace = fields.Char(required=True, index=True, readonly=True)
    value_ref = fields.Char(required=True, index=True, readonly=True, copy=False)
    comparison_hash = fields.Char(required=True, size=64, index=True, readonly=True)
    protected_value = fields.Char(
        required=True,
        readonly=True,
        copy=False,
        groups="marketing_center_base.group_marketing_center_admin",
    )
    observed_at = fields.Datetime(required=True, index=True, readonly=True)

    _sql_constraints = [
        (
            "event_namespace_unique",
            "unique(event_id, namespace)",
            "This click identifier namespace already exists for the event.",
        ),
        (
            "value_ref_unique",
            "unique(value_ref)",
            "The protected click identifier reference must be unique.",
        ),
        (
            "comparison_hash_sha256",
            "check(char_length(comparison_hash) = 64)",
            "The protected click identifier hash must be a SHA-256 digest.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("marketing_web_ingress_internal")
            is not WEB_INGRESS_INTERNAL_TOKEN
        ):
            raise AccessError(
                _("Protected click identifiers are created only by their service.")
            )
        return super().create(vals_list)

    def write(self, _values):  # pylint: disable=method-required-super
        raise AccessError(_("Protected click identifiers are immutable."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Protected click identifiers cannot be deleted."))

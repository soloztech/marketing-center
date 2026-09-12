import hashlib
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from odoo.addons.marketing_center_base.models.attribution import (
    ATTRIBUTION_ERASURE_TOKEN,
)

from ..services.tokens import WEB_INGRESS_INTERNAL_TOKEN

_WEB_RETENTION_TOKEN = object()

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
    retain_until = fields.Datetime(index=True, readonly=True, copy=False)
    retention_policy_version = fields.Char(readonly=True, copy=False)
    retention_assigned_by = fields.Many2one("res.users", readonly=True, copy=False)
    retention_assigned_at = fields.Datetime(readonly=True, copy=False)
    erased_at = fields.Datetime(index=True, readonly=True, copy=False)
    proposed_retain_until = fields.Datetime(compute="_compute_proposed_retain_until")

    @api.depends("retain_until", "observed_at", "endpoint_id.identifier_retention_days")
    def _compute_proposed_retain_until(self):
        for event in self:
            event.proposed_retain_until = (
                event.endpoint_id._retention_deadline(event.observed_at)
                if not event.retain_until
                and event.endpoint_id.identifier_retention_days > 0
                else False
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
            self.env.context.get("marketing_web_retention_token")
            is _WEB_RETENTION_TOKEN
        ):
            if set(values) == {"erased_at"} and values["erased_at"]:
                return super().write(values)
            if set(values) == {
                "retain_until",
                "retention_policy_version",
                "retention_assigned_by",
                "retention_assigned_at",
            } and all(not event.retain_until for event in self):
                return super().write(values)
            raise AccessError(_("Retention metadata can only be assigned once."))
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

    def _assign_retention_policy(self, endpoint, *, assigned_by):
        self.ensure_one()
        if self.endpoint_id != endpoint or self.company_id not in self.env.companies:
            raise AccessError(
                _("The retention policy belongs to another endpoint/company.")
            )
        if self.retain_until:
            return
        if not endpoint._privacy_policy_configured():
            raise AccessError(_("An explicit retention policy is required."))
        self.sudo().with_context(
            marketing_web_retention_token=_WEB_RETENTION_TOKEN
        ).write(
            {
                "retain_until": endpoint._retention_deadline(self.observed_at),
                "retention_policy_version": endpoint.privacy_policy_version,
                "retention_assigned_by": assigned_by,
                "retention_assigned_at": fields.Datetime.now(),
            }
        )
        self.touchpoint_id.sudo().identifier_ids._assign_retention_deadline(
            token=ATTRIBUTION_ERASURE_TOKEN,
            deadline=self.retain_until.date(),
        )

    @api.model
    def _cron_expire_retained_values(self, limit=100):
        """One bounded batch, with tombstones and per-company erasure scope."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AccessError(
                _("Retention cleanup accepts batches of 1 to 100 events.")
            )
        now = fields.Datetime.now()
        events = self.search(
            [
                ("state", "=", "done"),
                ("erased_at", "=", False),
                ("retain_until", "!=", False),
                ("retain_until", "<=", now),
                ("company_id", "in", self.env.companies.ids),
            ],
            order="retain_until, id",
            limit=limit,
        )
        for event in events:
            # Two cleanup jobs can race; the row lock serializes the destructive
            # projection and Odoo retries a stale REPEATABLE READ transaction.
            self.env.cr.execute(
                "SELECT id FROM marketing_web_ingress_event WHERE id = %s FOR UPDATE",
                [event.id],
            )
            event.invalidate_recordset(["erased_at"])
            if event.erased_at:
                continue
            scoped = event.sudo().with_context(
                allowed_company_ids=[event.company_id.id]
            )
            scoped.touchpoint_id._erase_private_values(
                token=ATTRIBUTION_ERASURE_TOKEN, now=now
            )
            scoped.click_value_ids._erase_private_values(
                token=_WEB_RETENTION_TOKEN, now=now
            )
            scoped._erase_related_private_values(now=now)
            scoped.with_context(
                marketing_web_retention_token=_WEB_RETENTION_TOKEN
            ).write({"erased_at": now})
        return len(events)

    def _erase_related_private_values(self, *, now):
        """Optional bridges erase their copies in the same retention transaction."""
        return True

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
    erased_at = fields.Datetime(index=True, readonly=True, copy=False)

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

    def _erase_private_values(self, *, token, now):
        if token is not _WEB_RETENTION_TOKEN or any(
            record.company_id not in self.env.companies for record in self
        ):
            raise AccessError(_("Click values require the scoped retention service."))
        for record in self.filtered(lambda item: not item.erased_at):
            super(MarketingWebIngressClickValue, record).write(
                {
                    "protected_value": "[erased]",
                    "comparison_hash": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                    "erased_at": now,
                }
            )

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Protected click identifiers cannot be deleted."))

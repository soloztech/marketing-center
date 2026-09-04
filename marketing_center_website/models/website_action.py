import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.contracts import (
    WebsiteActionContractError,
    route_ref,
    safe_relative_path,
    whatsapp_digits,
)


def _uuid(_recordset):
    return str(uuid.uuid4())


class MarketingWebsiteAction(models.Model):
    _name = "marketing.website.action"
    _description = "Marketing Website Technical Action"
    _order = "company_id, website_id, kind, name, id"
    _check_company_auto = True

    name = fields.Char(required=True, index=True)
    active = fields.Boolean(default=True, index=True)
    binding_id = fields.Many2one(
        "marketing.website.ingress.binding",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    website_id = fields.Many2one(
        related="binding_id.website_id",
        store=True,
        readonly=True,
        index=True,
    )
    company_id = fields.Many2one(
        related="binding_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    kind = fields.Selection(
        [
            ("form_submission", "Successful Website form"),
            ("whatsapp_handoff", "WhatsApp handoff"),
        ],
        required=True,
        index=True,
    )
    public_ref = fields.Char(
        required=True,
        default=_uuid,
        size=36,
        readonly=True,
        copy=False,
        index=True,
    )
    route_ref = fields.Char(
        required=True,
        size=128,
        help="Immutable technical route label; it must not contain customer data.",
    )
    source_path = fields.Char(
        required=True,
        default="/",
        size=512,
        help="Exact queryless Website path represented by this action.",
    )
    form_model_id = fields.Many2one(
        "ir.model",
        string="Website form model",
        domain="[('website_form_access', '=', True)]",
    )
    form_model_name = fields.Char(
        related="form_model_id.model",
        readonly=True,
    )
    whatsapp_destination = fields.Char(
        string="WhatsApp destination",
        size=15,
        groups="marketing_center_base.group_marketing_center_admin",
        help="Fixed E.164 destination using digits only; it is never sent to browser JS.",
    )
    fallback_path = fields.Char(
        required=True,
        default="/",
        size=512,
        help="Safe same-origin path used when a redirect grant cannot be consumed.",
    )
    token_ttl_seconds = fields.Integer(
        required=True,
        default=120,
        help="Lifetime of form receipts and WhatsApp redirect grants.",
    )
    marker_attribute = fields.Char(compute="_compute_marker_attribute")

    _sql_constraints = [
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The Website action public reference must be unique.",
        ),
        (
            "binding_kind_route_unique",
            "unique(binding_id, kind, route_ref)",
            "A Website binding cannot repeat a technical action route.",
        ),
        (
            "token_ttl_bounded",
            "check(token_ttl_seconds between 30 and 300)",
            "Website action tokens must live between 30 and 300 seconds.",
        ),
    ]

    @api.depends("kind", "public_ref")
    def _compute_marker_attribute(self):
        for action in self:
            attribute = (
                "data-marketing-form-action"
                if action.kind == "form_submission"
                else "data-marketing-whatsapp-action"
            )
            action.marker_attribute = '%s="%s"' % (attribute, action.public_ref or "")

    @api.model_create_multi
    def create(self, vals_list):
        self.check_access_rights("create")
        binding_ids = {
            values.get("binding_id") for values in vals_list if values.get("binding_id")
        }
        bindings = (
            self.env["marketing.website.ingress.binding"]
            .browse(sorted(binding_ids))
            .exists()
        )
        if set(bindings.ids) != binding_ids:
            raise ValidationError(_("The Website ingress binding is unavailable."))
        bindings._fence_effective_configuration()
        normalized = []
        for incoming in vals_list:
            values = dict(incoming)
            if "public_ref" in values:
                raise AccessError(_("Website action identity is managed internally."))
            values.update(self._normalized_values(values))
            normalized.append(values)
        records = super().create(normalized)
        records._lock_and_validate_binding()
        return records

    def write(self, values):
        self.check_access_rights("write")
        self.check_access_rule("write")
        values = dict(values)
        immutable = {
            "binding_id",
            "kind",
            "public_ref",
            "route_ref",
            "source_path",
            "form_model_id",
            "whatsapp_destination",
        }
        if immutable.intersection(values):
            raise AccessError(
                _(
                    "Website action identity is immutable. Archive this action and "
                    "create another one."
                )
            )
        if {"active", "fallback_path", "token_ttl_seconds"}.intersection(values):
            self.mapped("binding_id")._fence_effective_configuration()
        values.update(self._normalized_values(values))
        result = super().write(values)
        self._lock_and_validate_binding()
        return result

    @api.model
    def _normalized_values(self, values):
        normalized = {}
        try:
            if "route_ref" in values:
                normalized["route_ref"] = route_ref(values["route_ref"])
            if "source_path" in values:
                normalized["source_path"] = safe_relative_path(
                    values["source_path"], "source_path"
                )
            if "fallback_path" in values:
                normalized["fallback_path"] = safe_relative_path(
                    values["fallback_path"], "fallback_path"
                )
            if values.get("whatsapp_destination"):
                normalized["whatsapp_destination"] = whatsapp_digits(
                    values["whatsapp_destination"]
                )
        except WebsiteActionContractError as error:
            raise ValidationError(_("Invalid Website action: %s") % error) from error
        return normalized

    def _lock_and_validate_binding(self):
        for action in self.sorted("id"):
            self.env.cr.execute(
                "SELECT id FROM marketing_website_ingress_binding "
                "WHERE id = %s FOR SHARE",
                [action.binding_id.id],
            )
            action.binding_id.invalidate_recordset(
                ["active", "website_id", "company_id", "endpoint_id"]
            )
            if action.binding_id.endpoint_id.company_id != action.company_id:
                raise ValidationError(
                    _("The Website action and ingress endpoint must share a company.")
                )

    @api.constrains(
        "kind",
        "form_model_id",
        "whatsapp_destination",
        "route_ref",
        "source_path",
        "fallback_path",
        "token_ttl_seconds",
    )
    def _check_action_contract(self):
        for action in self:
            try:
                route_ref(action.route_ref)
                safe_relative_path(action.source_path, "source_path")
                safe_relative_path(action.fallback_path, "fallback_path")
                if action.kind == "form_submission":
                    if not action.form_model_id or action.whatsapp_destination:
                        raise WebsiteActionContractError(
                            "a form action requires only an allowed form model"
                        )
                    if not action.form_model_id.website_form_access:
                        raise WebsiteActionContractError(
                            "the selected model is not enabled for Website forms"
                        )
                elif action.kind == "whatsapp_handoff":
                    if action.form_model_id or not action.whatsapp_destination:
                        raise WebsiteActionContractError(
                            "a WhatsApp action requires only a fixed destination"
                        )
                    whatsapp_digits(action.whatsapp_destination)
                else:
                    raise WebsiteActionContractError("kind is invalid")
            except WebsiteActionContractError as error:
                raise ValidationError(
                    _("Invalid Website action: %s") % error
                ) from error

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Website actions must be archived instead of deleted."))

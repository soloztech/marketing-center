# Policy, signals and native UTM hooks are separate cooperative services.
# pylint: disable=consider-merging-classes-inherited
"""Protected policy and optional admission contract; no Contact dependency."""

import datetime
from contextlib import contextmanager

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.queue_job.exception import RetryableJobError

DEDUP_POLICY_TOKEN = object()
DEDUP_GROUPS = (
    "base.group_system",
    "marketing_center_base.group_marketing_center_admin",
    "contact_center_base.group_contact_center_admin",
)
POLICY_FIELDS = {
    "crm_cross_source_dedup_enabled",
    "crm_cross_source_window_hours",
    "crm_cross_source_reviewer_id",
}
AUDIT_FIELDS = {
    "crm_cross_source_revision",
    "crm_cross_source_enabled_at",
    "crm_cross_source_binding_watermark",
    "crm_cross_source_submission_watermark",
    "crm_cross_source_projection_watermark",
    "crm_cross_source_monitor_json",
}


class CrmDedupUnavailable(Exception):
    """Technical hold, never a human decision or permission to create blindly."""

    def __init__(self, reason="bridge_missing"):
        super().__init__(reason)
        self.reason = reason


def require_policy_admin(env, allow_missing_contact=False):
    if not all(
        (
            allow_missing_contact
            and group == "contact_center_base.group_contact_center_admin"
            and not env.ref(group, raise_if_not_found=False)
        )
        or (env.ref(group, raise_if_not_found=False) and env.user.has_group(group))
        for group in DEDUP_GROUPS
    ):
        raise AccessError(
            _("System, Marketing and Contact Center administration are all required.")
        )


class Company(models.Model):
    _inherit = "res.company"

    crm_cross_source_dedup_enabled = fields.Boolean(
        string="Reconciliar entradas Meta e WhatsApp",
        copy=False,
        groups="base.group_system",
    )
    crm_cross_source_window_hours = fields.Integer(
        string="Janela de coentrada em horas",
        default=24,
        groups="base.group_system",
    )
    crm_cross_source_reviewer_id = fields.Many2one(
        "res.users",
        string="Responsável pelas revisões CRM",
        groups="base.group_system",
        domain=[("share", "=", False)],
    )
    crm_cross_source_revision = fields.Integer(
        default=0, readonly=True, copy=False, groups="base.group_system"
    )
    crm_cross_source_enabled_at = fields.Datetime(
        readonly=True, copy=False, groups="base.group_system"
    )
    crm_cross_source_binding_watermark = fields.Integer(
        readonly=True, copy=False, groups="base.group_system"
    )
    crm_cross_source_submission_watermark = fields.Integer(
        readonly=True, copy=False, groups="base.group_system"
    )
    crm_cross_source_projection_watermark = fields.Integer(
        readonly=True, copy=False, groups="base.group_system"
    )
    crm_cross_source_monitor_json = fields.Json(
        compute="_compute_crm_cross_source_monitor", groups="base.group_system"
    )

    def _compute_crm_cross_source_monitor(self):
        rows = (
            self.env["marketing.crm.admission.monitor"]
            .sudo()
            .search([("company_id", "in", self.ids)])
        )
        by_company = {row.company_id.id: row.data_json for row in rows}
        for company in self:
            company.crm_cross_source_monitor_json = by_company.get(company.id, {})

    @api.model_create_multi
    def create(self, vals_list):
        self._crm_dedup_audit_guard(vals_list)
        defaults = {
            "crm_cross_source_dedup_enabled": False,
            "crm_cross_source_window_hours": 24,
            "crm_cross_source_reviewer_id": False,
        }
        configured = any(
            values.get(key, self.env.context.get("default_" + key, default)) != default
            for values in vals_list
            for key, default in defaults.items()
        )
        if configured:
            require_policy_admin(self.env)
        if any(
            values.get("crm_cross_source_dedup_enabled") for values in vals_list
        ) or self.env.context.get("default_crm_cross_source_dedup_enabled"):
            raise ValidationError(
                _("Create the company first, then activate its admission policy.")
            )
        return super().create(vals_list)

    def _crm_dedup_audit_guard(self, vals_list):
        if self.env.context.get("crm_dedup_policy_service") is DEDUP_POLICY_TOKEN:
            return
        if any(AUDIT_FIELDS.intersection(values) for values in vals_list) or any(
            "default_" + key in self.env.context for key in AUDIT_FIELDS
        ):
            raise AccessError(_("Admission policy audit is managed internally."))

    def write(self, values):
        self._crm_dedup_audit_guard([values])
        if not POLICY_FIELDS.intersection(values):
            return super().write(values)
        require_policy_admin(
            self.env,
            allow_missing_contact=values.get("crm_cross_source_dedup_enabled") is False,
        )
        self.check_access_rights("write")
        self.check_access_rule("write")
        if any(company not in self.env.companies for company in self):
            raise AccessError(_("The company is not active in this request."))
        # Bridge acquires and versions the shared fence before any company lock.
        for company in self.sorted("id"):
            service = self.env["marketing.crm.service"]
            if (
                values.get("crm_cross_source_dedup_enabled")
                and not service._crm_cross_source_capable()
            ):
                raise ValidationError(
                    _(
                        "Install the Contact CRM bridge before activating reconciliation."
                    )
                )
            force = (
                "disable"
                if values.get("crm_cross_source_dedup_enabled") is False
                and not service._crm_cross_source_capable()
                else True
            )
            with service._crm_cross_source_rpc_gate(company, force=force):
                self.env.cr.execute(
                    "SELECT id FROM res_company WHERE id=%s FOR UPDATE", [company.id]
                )
                company.invalidate_recordset()
                before = {
                    key: company[key].id if key.endswith("_id") else company[key]
                    for key in POLICY_FIELDS
                }
                super(Company, company).write(values)
                company._crm_dedup_validate_policy()
                if any(
                    before[key]
                    != (company[key].id if key.endswith("_id") else company[key])
                    for key in POLICY_FIELDS
                ):
                    audit = {
                        "crm_cross_source_revision": company.crm_cross_source_revision
                        + 1
                    }
                    if (
                        company.crm_cross_source_dedup_enabled
                        and not before["crm_cross_source_dedup_enabled"]
                    ):
                        audit[
                            "crm_cross_source_enabled_at"
                        ] = fields.Datetime.now() + datetime.timedelta(seconds=1)
                        for model, target in (
                            (
                                "contact.center.channel.binding",
                                "crm_cross_source_binding_watermark",
                            ),
                            (
                                "marketing.center.meta.lead.submission",
                                "crm_cross_source_submission_watermark",
                            ),
                            (
                                "marketing.center.meta.crm.projection",
                                "crm_cross_source_projection_watermark",
                            ),
                        ):
                            latest = (
                                self.env[model]
                                .sudo()
                                .with_context(active_test=False)
                                .search(
                                    [("company_id", "=", company.id)],
                                    order="id desc",
                                    limit=1,
                                )
                                if model in self.env.registry
                                else False
                            )
                            audit[target] = latest.id if latest else 0
                    company._crm_dedup_write_audit(audit)
        return True

    def _crm_dedup_write_audit(self, values):
        if set(values) - AUDIT_FIELDS:
            raise ValidationError(_("Only admission audit fields can be written here."))
        return (
            self.sudo()
            .with_context(crm_dedup_policy_service=DEDUP_POLICY_TOKEN)
            .write(values)
        )

    def _crm_dedup_validate_policy(self):
        for company in self.sudo():
            if not 1 <= company.crm_cross_source_window_hours <= 168:
                raise ValidationError(
                    _("Choose an admission window between 1 and 168 hours.")
                )
            if not company.crm_cross_source_dedup_enabled:
                continue
            reviewer = company.crm_cross_source_reviewer_id
            if not (
                self.env["marketing.crm.service"]._crm_cross_source_capable()
                and reviewer
                and reviewer.active
                and not reviewer.share
                and company in reviewer.company_ids
                and reviewer.has_group("sales_team.group_sale_manager")
                and self.env["crm.lead"]
                .with_user(reviewer)
                .check_access_rights("read", raise_exception=False)
            ):
                raise ValidationError(
                    _(
                        "Choose an active CRM manager from this company and install "
                        "the Contact CRM bridge."
                    )
                )


class Service(models.AbstractModel):
    _inherit = "marketing.crm.service"

    @api.model
    def _crm_cross_source_capable(self):
        return False

    @api.model
    @contextmanager
    def _crm_cross_source_rpc_gate(self, company, force=False):
        try:
            with self._crm_cross_source_gate(company, force=force) as service:
                yield service
        except (CrmDedupUnavailable, RetryableJobError) as error:
            raise ValidationError(
                _("Commercial intake is temporarily unavailable. Please retry shortly.")
            ) from error

    @api.model
    @contextmanager
    def _crm_cross_source_gate(self, company, force=False):
        if force == "disable":
            # Emergency deactivation cannot depend on the unavailable bridge.
            # Existing held entries remain held until an audited per-entry release.
            if "contact.center.crm.intake.gate" in self.env.registry:
                self.env["contact.center.crm.intake.gate"]._acquire_company(company)
            yield self
            return
        if force or company.sudo().crm_cross_source_dedup_enabled:
            raise CrmDedupUnavailable()
        yield self

    @api.model
    def _crm_cross_source_admit_meta(self, projection, values):
        if projection.company_id.sudo().crm_cross_source_dedup_enabled:
            raise CrmDedupUnavailable()
        return {"decision": "create", "lead_id": False, "reason": False, "keys": {}}

    @api.model
    def _crm_cross_source_check_gate(self, company):
        if company.sudo().crm_cross_source_dedup_enabled:
            raise CrmDedupUnavailable()
        return True

    @api.model
    def _crm_cross_source_intake_counts(self, company):
        return {"count": 0, "oldest": False}

    @api.model
    def _crm_cross_source_review_candidates(self, projection, values):
        return self.env["crm.lead"].browse()


class Module(models.Model):
    _inherit = "ir.module.module"

    def button_uninstall(self):
        if (self | self.downstream_dependencies()).filtered(
            lambda module: module.name
            in {"contact_center_crm", "marketing_center_contact_center"}
        ) and self.env["res.company"].sudo().search_count(
            [
                ("crm_cross_source_dedup_enabled", "=", True),
            ]
        ):
            raise ValidationError(
                _(
                    "Disable the cross-source admission policy explicitly before "
                    "removing its bridge."
                )
            )
        return super().button_uninstall()

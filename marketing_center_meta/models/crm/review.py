"""A limited native CRM queue without granting access to the Meta vault."""

import uuid

from odoo import Command, _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.marketing_center_base.models.crm.dedup_policy import (
    require_policy_admin,
)

from .tokens import META_REVIEW_TOKEN


def manager(env):
    if (
        not env.user.active
        or env.user.share
        or not env.user.has_group("sales_team.group_sale_manager")
    ):
        raise AccessError(
            _("Only an active internal CRM manager can resolve commercial intake.")
        )


class Projection(models.Model):
    _inherit = "marketing.center.meta.crm.projection"

    decision_actor_ref = fields.Integer(readonly=True, copy=False)
    resolution_decision = fields.Selection(
        [
            ("link", "Vincular"),
            ("new", "Demanda nova"),
            ("dismiss", "Encerrar"),
            ("release", "Liberar entrada técnica"),
        ],
        readonly=True,
        copy=False,
    )

    def _resolve_review(self, decision, lead_id=False, confirmed=False):
        manager(self.env)
        self.ensure_one()
        company = self.sudo().company_id.with_user(self.env.user)
        if (
            company not in self.env.companies
            or decision not in {"link", "new", "dismiss"}
            or type(confirmed) is not bool
            or not confirmed
        ):
            raise AccessError(_("Confirm the decision in the active company."))
        with self.env["marketing.crm.service"]._crm_cross_source_rpc_gate(
            company
        ) as service:
            projection = (
                self.sudo()
                .with_context(**service.env.context)
                .with_context(active_test=False)
            )
            projection.route_id._lock_crm_configuration()
            self.env.cr.execute(
                "SELECT id FROM marketing_center_meta_crm_projection WHERE id=%s FOR "
                "UPDATE NOWAIT",
                [projection.id],
            )
            projection.invalidate_recordset()
            if projection.state != "review":
                raise ValidationError(_("This review is no longer pending."))
            native = self.env["crm.lead"].with_context(
                active_test=False, allowed_company_ids=company.ids
            )
            native.check_access_rights("write")
            if decision in {
                "link",
                "new",
            } and not projection.route_id._crm_accepts_submission(
                projection.submission_id
            ):
                raise ValidationError(
                    _("This route no longer accepts this submission.")
                )
            lead = native.browse()
            if decision == "link":
                values = self.env["marketing.center.meta.crm.service"]._lead_values(
                    projection
                )
                current = service._crm_cross_source_review_candidates(
                    projection, values
                )
                if (
                    type(lead_id) is not int
                    or lead_id not in current.ids
                    or lead_id not in projection.review_candidate_ids.ids
                ):
                    raise ValidationError(
                        _("Choose a current candidate of this review.")
                    )
                lead = native.browse(lead_id).exists()
                lead.check_access_rights("read")
                lead.check_access_rule("read")
                lead.check_access_rule("write")
                if not lead or lead.company_id != company:
                    raise AccessError(_("The candidate is unavailable."))
                if "contact.center.crm.conversation.link" in self.env.registry:
                    links = (
                        self.env["contact.center.crm.conversation.link"]
                        .sudo()
                        .search([("lead_id", "=", lead.id), ("state", "=", "active")])
                    )
                    for channel in links.mapped("channel_id"):
                        self.env["contact.center.ui.api"]._crm_channel(
                            channel.id, mutate=True
                        )
                    lead._contact_center_lock_conversation_graph(
                        touch_leads=True, touch_channels=True
                    )
                    lead.invalidate_recordset()
                    lead.check_access_rule("read")
                    lead.check_access_rule("write")
                    current = service._crm_cross_source_review_candidates(
                        projection, values
                    )
                    if lead.id not in current.ids:
                        raise ValidationError(
                            _("The candidate changed. Reload this review.")
                        )
            elif decision == "new":
                native.check_access_rights("create")
                if not projection.route_id._crm_accepts_submission(
                    projection.submission_id
                ):
                    raise ValidationError(
                        _("This route no longer accepts this submission.")
                    )
            audit = {
                "review_decision_ref": str(uuid.uuid4()),
                "reviewer_id": self.env.uid,
                "decision_actor_ref": self.env.uid,
                "decided_at": fields.Datetime.now(),
                "resolution_decision": decision,
            }
            if decision == "dismiss":
                projection._internal_write(
                    {
                        **audit,
                        "state": "dismissed",
                        "admission_decision": "dismissed",
                        "queue_job_uuid": False,
                    }
                )
            else:
                projection._internal_write({**audit, "state": "processing"})
                self.env["marketing.center.meta.crm.service"].with_context(
                    **service.env.context,
                    meta_crm_review_token=META_REVIEW_TOKEN,
                    meta_crm_review_decision={
                        "decision": "reuse" if lead else "create",
                        "lead_id": lead.id or False,
                        "identity": "exact_phone"
                        if projection.comparison_exact
                        else "unverified",
                    },
                )._project(projection)
            return True

    def _release_technical_hold(self):
        """Audited per-entry escape only after an explicit administrator action."""
        require_policy_admin(self.env)
        self.ensure_one()
        company = self.sudo().company_id.with_user(self.env.user)
        company.check_access_rights("write")
        company.check_access_rule("write")
        if company not in self.env.companies or company.crm_cross_source_dedup_enabled:
            raise ValidationError(
                _(
                    "Technical release requires the protection to be explicitly disabled."
                )
            )
        with self.env["marketing.crm.service"]._crm_cross_source_rpc_gate(company):
            self.env.cr.execute(
                "SELECT id FROM marketing_center_meta_crm_projection WHERE id=%s FOR "
                "UPDATE NOWAIT",
                [self.id],
            )
            projection = self.sudo()
            projection.invalidate_recordset()
            if projection.state != "pending" or not projection.technical_hold_reason:
                raise ValidationError(
                    _("This entry has no outstanding technical hold.")
                )
            projection._internal_write(
                {
                    "technical_hold_reason": False,
                    "technical_hold_since": False,
                    "next_technical_retry_at": False,
                    "technical_release_ref": str(uuid.uuid4()),
                    "review_decision_ref": str(uuid.uuid4()),
                    "reviewer_id": self.env.uid,
                    "decision_actor_ref": self.env.uid,
                    "decided_at": fields.Datetime.now(),
                    "resolution_decision": "release",
                }
            )
            projection._enqueue()
        return True


class ReviewQueue(models.TransientModel):
    _name = "marketing.center.meta.crm.review.queue"
    _description = "Entradas comerciais a revisar"

    company_id = fields.Many2one("res.company", readonly=True, required=True)
    offset = fields.Integer(readonly=True, default=0)
    total = fields.Integer(readonly=True)
    line_ids = fields.One2many(
        "marketing.center.meta.crm.review.line", "queue_id", readonly=True
    )

    @api.model_create_multi
    def create(self, values_list):
        if self.env.context.get("meta_crm_review_token") is not META_REVIEW_TOKEN:
            raise AccessError(_("Open commercial reviews from the CRM menu."))
        manager(self.env)
        return super().create(values_list)

    def write(self, values):
        if self.env.context.get("meta_crm_review_token") is not META_REVIEW_TOKEN:
            raise AccessError(_("The commercial review queue is managed internally."))
        return super().write(values)

    @api.model
    def action_open(self):
        manager(self.env)
        queue = self.with_context(meta_crm_review_token=META_REVIEW_TOKEN).create(
            {"company_id": self.env.company.id}
        )
        queue._load_page()
        return queue._action()

    def _action(self):
        return {
            "type": "ir.actions.act_window",
            "name": _("Entradas a revisar"),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    def _load_page(self):
        self.ensure_one()
        manager(self.env)
        self.check_access_rights("read")
        self.check_access_rule("read")
        if self.company_id not in self.env.companies:
            raise AccessError(_("The review company is unavailable."))
        domain = [
            ("company_id", "=", self.company_id.id),
            "|",
            ("state", "=", "review"),
            ("technical_hold_reason", "!=", False),
        ]
        Projection = self.env["marketing.center.meta.crm.projection"].sudo()
        rows = Projection.search(
            domain, order="create_date, id", offset=max(self.offset, 0), limit=20
        )
        commands = [Command.clear()]
        service = self.env["marketing.center.meta.crm.service"]
        for row in rows:
            values = service._lead_values(row)
            commands.append(
                Command.create(
                    {
                        "projection_ref": row.public_ref,
                        "name": values.get("contact_name") or values["name"],
                        "phone": values.get("phone"),
                        "email": values.get("email_from"),
                        "reason": row.review_reason or row.technical_hold_reason,
                        "technical": bool(row.technical_hold_reason),
                        "route_label": row.route_id.name,
                        "team_label": row.route_id.crm_team_id.name or False,
                    }
                )
            )
        self.with_context(meta_crm_review_token=META_REVIEW_TOKEN).write(
            {"total": Projection.search_count(domain), "line_ids": commands}
        )

    def action_next(self):
        self.ensure_one()
        self.with_context(meta_crm_review_token=META_REVIEW_TOKEN).write(
            {"offset": min(self.offset + 20, max(self.total - 1, 0))}
        )
        self._load_page()
        return self._action()

    def action_previous(self):
        self.ensure_one()
        self.with_context(meta_crm_review_token=META_REVIEW_TOKEN).write(
            {"offset": max(self.offset - 20, 0)}
        )
        self._load_page()
        return self._action()

    def action_refresh(self):
        self._load_page()
        return self._action()


class ReviewLine(models.TransientModel):
    _name = "marketing.center.meta.crm.review.line"
    _description = "Revisão de entrada comercial"

    queue_id = fields.Many2one(
        "marketing.center.meta.crm.review.queue",
        required=True,
        readonly=True,
        ondelete="cascade",
    )
    projection_ref = fields.Char(readonly=True, required=True)
    company_id = fields.Many2one(
        "res.company", related="queue_id.company_id", store=True, readonly=True
    )
    name = fields.Char(readonly=True)
    phone = fields.Char(readonly=True)
    email = fields.Char(readonly=True)
    reason = fields.Char(readonly=True)
    route_label = fields.Char(readonly=True)
    team_label = fields.Char(readonly=True)
    technical = fields.Boolean(readonly=True)
    candidate_ids = fields.Many2many(
        "crm.lead", compute="_compute_candidates", context={"active_test": False}
    )
    lead_id = fields.Many2one(
        "crm.lead",
        string="Negócio confirmado",
        domain="[('id', 'in', candidate_ids)]",
        context={"active_test": False},
    )
    confirmed = fields.Boolean(string="Confirmo a decisão para esta entrada")

    def _projection(self):
        self.ensure_one()
        manager(self.env)
        self.check_access_rights("read")
        self.check_access_rule("read")
        if self.queue_id.company_id not in self.env.companies:
            raise AccessError(_("The review company is unavailable."))
        projection = (
            self.env["marketing.center.meta.crm.projection"]
            .sudo()
            .search(
                [
                    ("public_ref", "=", self.projection_ref),
                    ("company_id", "=", self.queue_id.company_id.id),
                ],
                limit=1,
            )
        )
        if not projection:
            raise AccessError(_("The review entry is unavailable."))
        return projection.with_user(self.env.user)

    @api.depends("projection_ref", "queue_id.company_id")
    def _compute_candidates(self):
        for line in self:
            line.candidate_ids = line._current_candidates()

    def _current_candidates(self):
        self.ensure_one()
        projection = self._projection().sudo().with_context(active_test=False)
        values = self.env["marketing.center.meta.crm.service"]._lead_values(projection)
        current = self.env["marketing.crm.service"]._crm_cross_source_review_candidates(
            projection, values
        )
        ids = (current & projection.review_candidate_ids).ids
        return (
            self.env["crm.lead"]
            .with_context(active_test=False)
            .search(
                [("id", "in", ids), ("company_id", "=", self.queue_id.company_id.id)]
            )
        )

    @api.model_create_multi
    def create(self, values_list):
        if self.env.context.get("meta_crm_review_token") is not META_REVIEW_TOKEN:
            raise AccessError(_("Review items are prepared internally."))
        return super().create(values_list)

    def _validate_selected_lead(self, lead_id):
        if not lead_id:
            return
        if type(lead_id) is not int:
            raise ValidationError(_("Choose an available review candidate."))
        for line in self:
            if lead_id not in line._current_candidates().ids:
                raise AccessError(_("The candidate is unavailable for this review."))
            lead = self.env["crm.lead"].with_context(active_test=False).browse(lead_id)
            lead.check_access_rights("read")
            lead.check_access_rule("read")
            lead.check_access_rights("write")
            lead.check_access_rule("write")

    def onchange(self, values, field_name, field_onchange):
        # Validate before Odoo serializes a Many2one display name.
        if values.get("lead_id"):
            if not self.ids or any(not isinstance(row_id, int) for row_id in self.ids):
                raise AccessError(_("Reload the review before selecting a candidate."))
            self._validate_selected_lead(values["lead_id"])
        return super().onchange(values, field_name, field_onchange)

    def write(self, values):
        if self.env.context.get(
            "meta_crm_review_token"
        ) is not META_REVIEW_TOKEN and set(values) - {"lead_id", "confirmed"}:
            raise AccessError(_("Review source fields cannot be edited."))
        if "lead_id" in values:
            self._validate_selected_lead(values["lead_id"])
        return super().write(values)

    def action_open(self):
        self.ensure_one()
        manager(self.env)
        self.check_access_rights("read")
        self.check_access_rule("read")
        self._projection()
        return {
            "type": "ir.actions.act_window",
            "name": _("Revisar entrada"),
            "res_model": self._name,
            "res_id": self.id,
            "views": [
                (
                    self.env.ref(
                        "marketing_center_meta.view_meta_crm_review_line_form"
                    ).id,
                    "form",
                )
            ],
            "target": "new",
            "context": dict(self.env.context, form_view_initial_mode="edit"),
        }

    def action_link(self):
        self._projection()._resolve_review("link", self.lead_id.id, self.confirmed)
        return self.queue_id.action_refresh()

    def action_new(self):
        self._projection()._resolve_review("new", confirmed=self.confirmed)
        return self.queue_id.action_refresh()

    def action_dismiss(self):
        self._projection()._resolve_review("dismiss", confirmed=self.confirmed)
        return self.queue_id.action_refresh()

    def action_retry_technical(self):
        projection = self._projection().sudo()
        if not projection.technical_hold_reason or projection.state != "pending":
            raise ValidationError(
                _("This entry is no longer waiting for technical recovery.")
            )
        projection._enqueue()
        return self.queue_id.action_refresh()

    def action_release_technical(self):
        if not self.confirmed:
            raise ValidationError(_("Confirm the release of this entry."))
        self._projection()._release_technical_hold()
        return self.queue_id.action_refresh()

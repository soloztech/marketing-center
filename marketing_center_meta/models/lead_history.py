"""Explicit, auditable history requests over the existing Lead Ads sweep."""

import datetime

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_HISTORY_TOKEN = object()
_PERIOD_TOKEN = object()
_PERIOD_INTERNAL_FIELDS = {"initial_sync_new_only", "initial_sync_started_at"}
_PERIODS = [
    ("7", "Últimos 7 dias"),
    ("30", "Últimos 30 dias"),
    ("90", "Últimos 90 dias"),
]


class MarketingCenterMetaLeadRoute(models.Model):
    _inherit = "marketing.center.meta.lead.route"

    initial_sync_period = fields.Selection(
        [
            ("new", "Somente novas entradas"),
            *_PERIODS,
            ("legacy", "Período anterior preservado"),
        ],
        string="Período inicial de coleta",
        compute="_compute_initial_sync_period",
        inverse="_inverse_initial_sync_period",
        help=(
            "Define a primeira coleta deste formulário. Para buscar um período "
            "anterior depois da primeira coleta, use Sincronizar histórico."
        ),
    )
    initial_sync_new_only = fields.Boolean(readonly=True, copy=False)
    initial_sync_started_at = fields.Datetime(readonly=True, copy=False)
    initial_sync_period_locked = fields.Boolean(
        compute="_compute_initial_sync_period_locked",
    )
    history_request_id = fields.Many2one(
        "marketing.center.meta.lead.history",
        readonly=True,
        copy=False,
        ondelete="restrict",
        check_company=True,
    )
    history_ids = fields.One2many(
        "marketing.center.meta.lead.history",
        "route_id",
        string="Sincronizações de histórico",
        readonly=True,
    )

    @api.model_create_multi
    def create(self, vals_list):
        self._check_period_defaults()
        for values in vals_list:
            if _PERIOD_INTERNAL_FIELDS.intersection(values):
                raise AccessError(_("O início da coleta é controlado pelo sistema."))
            period = values.get(
                "initial_sync_period",
                self.env.context.get("default_initial_sync_period"),
            )
            if period is not None:
                self._check_initial_period_value(period)
        return super().create(vals_list)

    def write(self, values):
        internal_period = (
            self.env.context.get("marketing_lead_period_token") is _PERIOD_TOKEN
        )
        if _PERIOD_INTERNAL_FIELDS.intersection(values) and not internal_period:
            raise AccessError(_("O início da coleta é controlado pelo sistema."))
        if "initial_sync_period" in values:
            self._check_initial_period_value(values["initial_sync_period"])
            self._check_admin()
            for route in self.sorted("id"):
                route._lock_reconciliation_schedule()
                if route._initial_collection_started():
                    raise UserError(
                        _(
                            "A coleta já começou. Use Sincronizar histórico "
                            "para buscar outro período."
                        )
                    )
        histories = self.mapped("history_request_id")
        result = super().write(values)
        if histories and {
            "active",
            "lead_profile_id",
            "source_id",
            "reconcile_enabled",
        }.intersection(values):
            histories._internal_write({"state": "paused"})
        return result

    @api.model
    def _check_period_defaults(self):
        managed = _PERIOD_INTERNAL_FIELDS | {"history_request_id"}
        if any("default_%s" % field in self.env.context for field in managed):
            raise AccessError(_("O início da coleta é controlado pelo sistema."))

    @api.model
    def _check_initial_period_value(self, period):
        if period not in {"new", "7", "30", "90"}:
            raise ValidationError(
                _("Escolha somente novas entradas ou um período de 7, 30 ou 90 dias.")
            )

    def _initial_collection_started(self):
        self.ensure_one()
        if not self.id:
            return False
        return bool(
            self.last_reconciled_at
            or self.reconcile_since
            or self.reconcile_job_uuid
            or self.reconcile_state in {"queued", "running", "partial", "error"}
            or self.history_ids.sudo().filtered(
                lambda history: history.state != "draft"
            )
            or self.env["marketing.center.meta.lead.submission"]
            .sudo()
            .search_count(
                [
                    ("route_id", "=", self.id),
                ]
            )
        )

    @api.depends(
        "last_reconciled_at",
        "reconcile_since",
        "reconcile_job_uuid",
        "reconcile_state",
        "history_ids.state",
        "submission_ids",
    )
    def _compute_initial_sync_period_locked(self):
        for route in self:
            route.initial_sync_period_locked = route._initial_collection_started()

    @api.depends("reconcile_lookback_hours", "initial_sync_new_only")
    def _compute_initial_sync_period(self):
        periods = {168: "7", 720: "30", 2160: "90"}
        for route in self:
            route.initial_sync_period = (
                "new"
                if route.initial_sync_new_only
                else periods.get(route.reconcile_lookback_hours, "legacy")
            )

    def _inverse_initial_sync_period(self):
        for route in self:
            period = route.initial_sync_period
            if period == "legacy":
                continue
            if period == "new":
                route.with_context(marketing_lead_period_token=_PERIOD_TOKEN).write(
                    {
                        "initial_sync_new_only": True,
                        "initial_sync_started_at": route.initial_sync_started_at
                        or fields.Datetime.now(),
                    }
                )
            else:
                route.with_context(marketing_lead_period_token=_PERIOD_TOKEN).write(
                    {
                        "initial_sync_new_only": False,
                        "initial_sync_started_at": False,
                        "reconcile_lookback_hours": int(period) * 24,
                    }
                )

    @api.model
    def _runtime_fields(self):
        return super()._runtime_fields() | {"history_request_id"}

    def _new_reconcile_since(self):
        self.ensure_one()
        if self.history_request_id:
            return self.history_request_id.requested_since
        since = super()._new_reconcile_since()
        if self.initial_sync_new_only:
            # The ordinary overlap must not import leads older than a route
            # explicitly configured to begin with new entries only.
            started_at = self.initial_sync_started_at or self.create_date
            return max(since, started_at) if self.last_reconciled_at else started_at
        return since

    def _continue_reconciliation_sweep(self):
        self.ensure_one()
        return (
            bool(self.history_request_id and self.reconcile_since)
            or super()._continue_reconciliation_sweep()
        )

    def _runtime_write(self, values):
        self.ensure_one()
        history = self.history_request_id
        values = dict(values)
        if history and values.get("reconcile_state"):
            state = values["reconcile_state"]
            if state == "idle" and "last_reconciled_at" in values:
                # A backfill must not advance or rewind the incremental cursor.
                # In particular a short history request cannot skip an older gap.
                values["last_reconciled_at"] = history.previous_reconciled_at
                values["history_request_id"] = False
                history._internal_write(
                    {"state": "done", "finished_at": fields.Datetime.now()}
                )
            elif state in {"queued", "running", "partial", "error", "paused"}:
                history._internal_write({"state": state})
        return super()._runtime_write(values)

    def _check_history_access(self):
        self.ensure_one()
        self._check_admin()
        self.check_access_rights("write")
        self.check_access_rule("write")
        if self.company_id not in self.env.companies:
            raise AccessError(_("A empresa deste formulário não está disponível."))

    def action_open_history_sync(self):
        self._check_history_access()
        return {
            "type": "ir.actions.act_window",
            "name": _("Sincronizar histórico"),
            "res_model": "marketing.center.meta.lead.history",
            "view_mode": "form",
            "target": "new",
            "context": {"default_route_id": self.id},
        }


class MarketingCenterMetaLeadHistory(models.Model):
    _name = "marketing.center.meta.lead.history"
    _description = "Sincronização de histórico dos formulários Meta"
    _order = "id desc"
    _check_company_auto = True
    _rec_name = "route_id"

    route_id = fields.Many2one(
        "marketing.center.meta.lead.route",
        string="Formulário",
        required=True,
        ondelete="restrict",
        check_company=True,
    )
    company_id = fields.Many2one(
        related="route_id.company_id", store=True, readonly=True
    )
    period = fields.Selection(
        [*_PERIODS, ("custom", "Desde uma data")],
        string="Período",
        required=True,
        default="7",
    )
    start_date = fields.Date(string="Data inicial")
    requested_since = fields.Datetime(string="Buscar desde", readonly=True, copy=False)
    requested_at = fields.Datetime(
        string="Buscar até / solicitado em", readonly=True, copy=False
    )
    requested_by = fields.Many2one(
        "res.users", string="Solicitado por", readonly=True, copy=False
    )
    previous_reconciled_at = fields.Datetime(readonly=True, copy=False)
    finished_at = fields.Datetime(string="Concluído em", readonly=True, copy=False)
    state = fields.Selection(
        [
            ("draft", "Preparar"),
            ("queued", "Na fila"),
            ("running", "Buscando"),
            ("partial", "Aguardando continuação"),
            ("done", "Concluído"),
            ("error", "Requer atenção"),
            ("paused", "Pausado"),
        ],
        string="Situação",
        default="draft",
        required=True,
        readonly=True,
        copy=False,
    )

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.user.has_group(
            "marketing_center_base.group_marketing_center_admin"
        ):
            raise AccessError(
                _("Somente administradores podem sincronizar o histórico.")
            )
        allowed = {"route_id", "period", "start_date"}
        managed = set(self._fields) - allowed
        if any("default_%s" % field in self.env.context for field in managed):
            raise AccessError(_("O estado da sincronização é controlado pelo sistema."))
        for values in vals_list:
            if set(values) - allowed:
                raise AccessError(
                    _("O estado da sincronização é controlado pelo sistema.")
                )
            route = (
                self.env["marketing.center.meta.lead.route"]
                .browse(
                    values.get("route_id") or self.env.context.get("default_route_id")
                )
                .exists()
            )
            route._check_history_access()
        return super().create(vals_list)

    def write(self, values):
        if self.env.context.get("marketing_lead_history_token") is _HISTORY_TOKEN:
            return super().write(values)
        for history in self:
            history.route_id._check_history_access()
            if history.state != "draft" or set(values) - {"period", "start_date"}:
                raise AccessError(_("Uma solicitação enviada não pode ser alterada."))
        return super().write(values)

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("O registro de sincronização deve ser preservado."))

    def _internal_write(self, values):
        return self.with_context(marketing_lead_history_token=_HISTORY_TOKEN).write(
            values
        )

    def _cutoff(self, now):
        self.ensure_one()
        if self.period != "custom":
            return now - datetime.timedelta(days=int(self.period))
        if not self.start_date:
            raise ValidationError(_("Informe a data inicial do histórico."))
        timezone = pytz.timezone(
            self.env.context.get("tz") or self.env.user.tz or "UTC"
        )
        since = timezone.localize(
            datetime.datetime.combine(self.start_date, datetime.time.min)
        )
        since = since.astimezone(pytz.UTC).replace(tzinfo=None)
        if since > now or since < now - datetime.timedelta(days=90):
            raise ValidationError(
                _("Escolha uma data passada dentro dos últimos 90 dias.")
            )
        return since

    def action_sync(self):
        self.ensure_one()
        self.check_access_rights("write")
        self.check_access_rule("write")
        route = self.route_id
        route._check_history_access()
        route._lock_reconciliation_schedule()
        self.invalidate_recordset()
        if self.state != "draft":
            raise UserError(_("Esta solicitação já foi enviada."))
        if (
            route.history_request_id
            or route._current_reconcile_job()
            or route.reconcile_state in {"queued", "running", "partial"}
        ):
            raise UserError(
                _(
                    "Já existe uma coleta em andamento para este formulário. "
                    "Aguarde sua conclusão."
                )
            )
        if not (
            route.active
            and route.source_id.active
            and route.reconcile_enabled
            and route.webhook_page_id.active
            and route.webhook_page_id.endpoint_id.active
            and route.lead_profile_id.active
            and route.meta_app_id.active
        ):
            raise UserError(
                _(
                    "Ative o formulário, suas conexões e a sincronização "
                    "antes de buscar o histórico."
                )
            )
        now = fields.Datetime.now()
        since = self._cutoff(now)
        self._internal_write(
            {
                "requested_since": since,
                "requested_at": now,
                "requested_by": self.env.uid,
                "previous_reconciled_at": route.last_reconciled_at,
                "state": "queued",
            }
        )
        route._runtime_write(
            {
                "history_request_id": self.id,
                "reconcile_state": "partial",
                "reconcile_since": since,
                "reconcile_after": False,
                "reconcile_cursor_hashes_json": [route._reconcile_cursor_digest("")],
                "reconcile_page_count": 0,
                "reconcile_attempts": 0,
                "reconcile_job_uuid": False,
            }
        )
        route._enqueue_reconciliation()
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }


class MarketingCenterMetaLeadService(models.AbstractModel):
    _inherit = "marketing.center.meta.lead.service"

    @api.model
    def _project_reconcile_leads(self, route, leads, **kwargs):
        history = route.history_request_id
        if history:
            leads = tuple(
                lead
                for lead in leads
                if history.requested_since <= lead.created_at <= history.requested_at
            )
        elif route.initial_sync_new_only:
            leads = tuple(
                lead
                for lead in leads
                if lead.created_at
                >= (route.initial_sync_started_at or route.create_date)
            )
        return super()._project_reconcile_leads(route, leads, **kwargs)

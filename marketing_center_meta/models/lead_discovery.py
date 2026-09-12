"""Explicit discovery and selection of Meta forms using an existing webhook."""

import datetime

from psycopg2 import errors as pg_errors

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from odoo.addons.marketing_center_base.services.dto import canonical_json, sha256_text
from odoo.addons.marketing_center_base.services.serialization import (
    MarketingSerializationFailure,
    acquire_advisory_xact_lock,
)
from odoo.addons.meta_api_base.services.errors import MetaApiError

from ..services.adapter import META_ADAPTER_KEY, META_ADS_SERVICE
from ..services.lead_forms import MetaLeadFormsReadAdapter

_DISCOVERY_TOKEN = object()
_CONTEXT_KEY = "marketing_meta_form_discovery_internal"
_SCOPE_FIELDS = {"company_id", "webhook_page_id", "lead_profile_id", "source_id"}
_RESULT_FIELDS = {"line_ids", "discovered_at", "scope_fingerprint", "state"}
_MAX_PREVIEW_AGE = datetime.timedelta(minutes=30)


def _internal(records):
    return records.env.context.get(_CONTEXT_KEY) is _DISCOVERY_TOKEN


def _check_admin(records):
    if not records.env.user.has_group(
        "marketing_center_base.group_marketing_center_admin"
    ):
        raise AccessError(
            _("Somente administradores de Marketing podem descobrir formulários.")
        )


class MarketingCenterMetaLeadDiscovery(models.TransientModel):
    _name = "marketing.center.meta.lead.discovery"
    _description = "Descobrir formulários Meta"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company",
        string="Empresa",
        required=True,
        default=lambda self: self.env.company,
    )
    webhook_page_id = fields.Many2one(
        "meta.webhook.page", string="Página Meta", required=True, check_company=True
    )
    lead_profile_id = fields.Many2one(
        "marketing.center.meta.profile",
        string="Perfil de leitura dos formulários",
        required=True,
        check_company=True,
        domain="[('company_id', '=', company_id), ('reader_kind', '=', 'lead_reader')]",
    )
    source_id = fields.Many2one(
        "marketing.center.source",
        string="Fonte de marketing",
        required=True,
        check_company=True,
        domain="[('company_id', '=', company_id), ('service', '=', 'meta.ads')]",
    )
    initial_sync_period = fields.Selection(
        [
            ("new", "Somente novos, a partir da configuração"),
            ("7", "Últimos 7 dias"),
            ("30", "Últimos 30 dias"),
            ("90", "Últimos 90 dias"),
        ],
        string="Período inicial",
        required=True,
        default="new",
        help="Aplica-se somente às novas rotas selecionadas. Não altera rotas existentes.",
    )
    line_ids = fields.One2many(
        "marketing.center.meta.lead.discovery.line",
        "discovery_id",
        string="Formulários",
    )
    discovered_at = fields.Datetime(string="Consultado em", readonly=True)
    scope_fingerprint = fields.Char(readonly=True, size=64)
    state = fields.Selection(
        [
            ("draft", "Escolher página"),
            ("preview", "Conferir formulários"),
            ("done", "Configurado"),
        ],
        required=True,
        default="draft",
        readonly=True,
    )

    @api.model
    def default_get(self, fields_list):
        _check_admin(self)
        values = super().default_get(fields_list)
        company_id = values.get("company_id") or self.env.context.get(
            "default_company_id", self.env.company.id
        )
        if company_id not in self.env.companies.ids:
            raise AccessError(_("A empresa não está autorizada nesta sessão."))
        scope_names = ("webhook_page_id", "lead_profile_id", "source_id")
        domain = [
            ("company_id", "=", company_id),
            ("active", "=", True),
            ("webhook_page_id.active", "=", True),
            ("webhook_page_id.endpoint_id.active", "=", True),
            ("webhook_page_id.app_id.active", "=", True),
            ("lead_profile_id.active", "=", True),
            ("source_id.active", "=", True),
            ("source_id.read_enabled", "=", True),
            ("source_id.state", "=", "active"),
        ]
        for name in scope_names:
            explicit = self.env.context.get("default_%s" % name)
            if explicit:
                domain.append((name, "=", explicit))
        # Group on the server: two combinations suffice to prove ambiguity even
        # on Pages with thousands of existing routes, without loading them all.
        combinations = self.env["marketing.center.meta.lead.route"].read_group(
            domain, list(scope_names), list(scope_names), limit=2, lazy=False
        )
        if len(combinations) == 1:
            combination = combinations[0]
            for name in scope_names:
                if name in fields_list and "default_%s" % name not in self.env.context:
                    pair = combination.get(name)
                    if pair:
                        values[name] = pair[0]
        return values

    @api.model_create_multi
    def create(self, vals_list):
        _check_admin(self)
        if not _internal(self):
            forged_defaults = any(
                "default_%s" % field_name in self.env.context
                for field_name in _RESULT_FIELDS
            )
            if forged_defaults or any(
                (_RESULT_FIELDS - {"line_ids"}).intersection(v) or v.get("line_ids")
                for v in vals_list
            ):
                raise AccessError(
                    _("O resultado da descoberta é preenchido pelo sistema.")
                )
        records = super().create(vals_list)
        records._check_scope()
        return records

    def write(self, values):
        _check_admin(self)
        values = dict(values)
        if not _internal(self):
            if (_RESULT_FIELDS - {"line_ids"}).intersection(values):
                raise AccessError(
                    _("O resultado da descoberta é preenchido pelo sistema.")
                )
            if "line_ids" in values:
                self.ensure_one()
            allowed_lines = set(self.line_ids.ids)
            updates = []
            for command in values.get("line_ids", []):
                if (
                    not isinstance(command, (tuple, list))
                    or len(command) != 3
                    or command[1] not in allowed_lines
                ):
                    raise AccessError(
                        _("Somente a seleção dos formulários pode ser alterada.")
                    )
                # The web client also sends LINK for unchanged existing rows.
                # Ignore only links already owned by this wizard: forwarding
                # them would unnecessarily invoke the protected inverse field.
                if command[0] == 4 and command[2] in (0, False):
                    continue
                if (
                    command[0] != 1
                    or not isinstance(command[2], dict)
                    or set(command[2]) != {"selected"}
                ):
                    raise AccessError(
                        _("Somente a seleção dos formulários pode ser alterada.")
                    )
                updates.append(command)
            if "line_ids" in values:
                values["line_ids"] = updates
        if not _internal(self) and _SCOPE_FIELDS.intersection(values):
            values.update(
                {
                    "line_ids": [(5, 0, 0)],
                    "discovered_at": False,
                    "scope_fingerprint": False,
                    "state": "draft",
                }
            )
        result = super(MarketingCenterMetaLeadDiscovery, self._internal()).write(values)
        self._check_scope()
        return result

    def _internal(self):
        return self.with_context(**{_CONTEXT_KEY: _DISCOVERY_TOKEN})

    def _check_scope(self):
        _check_admin(self)
        for wizard in self:
            if wizard.company_id not in self.env.companies:
                raise AccessError(_("A empresa não está autorizada nesta sessão."))
            page = wizard.webhook_page_id
            profile = wizard.lead_profile_id
            source = wizard.source_id
            for record in (page, page.endpoint_id, page.app_id, profile, source):
                record.check_access_rights("read")
                record.check_access_rule("read")
            if (
                not page
                or not profile
                or not source
                or page.company_id != wizard.company_id
                or page.endpoint_id.company_id != wizard.company_id
                or page.app_id.company_id != wizard.company_id
                or profile.company_id != wizard.company_id
                or source.company_id != wizard.company_id
                or profile.meta_app_id != page.app_id
                or profile.reader_kind != "lead_reader"
                or source.service != META_ADS_SERVICE
            ):
                raise ValidationError(
                    _(
                        "Página, perfil e fonte precisam pertencer à mesma empresa "
                        "e ao mesmo aplicativo Meta."
                    )
                )
            if (
                not page.active
                or not page.endpoint_id.active
                or not page.app_id.active
                or not profile.active
                or not source.active
                or not source.read_enabled
                or source.state != "active"
            ):
                raise ValidationError(
                    _(
                        "Ative a página, o perfil e a leitura da fonte antes de continuar."
                    )
                )
            connections = source.connection_ids.filtered(
                lambda connection: connection.active
                and connection.adapter_key == META_ADAPTER_KEY
                and connection.purpose == "reader"
                and connection.meta_profile_id.active
                and connection.meta_profile_id.meta_app_id == page.app_id
            )
            if not connections:
                raise ValidationError(
                    _(
                        "A fonte precisa ter uma conexão de leitura Meta "
                        "do mesmo aplicativo da página."
                    )
                )
        return True

    def _fingerprint(self):
        self.ensure_one()
        page, profile, source = (
            self.webhook_page_id,
            self.lead_profile_id,
            self.source_id,
        )
        return sha256_text(
            canonical_json(
                {
                    "company": self.company_id.id,
                    "page": [page.id, page.external_page_id, page.revision],
                    "endpoint": [page.endpoint_id.id, page.endpoint_id.revision],
                    "app": [page.app_id.id, page.app_id.revision],
                    "profile": [profile.id, profile.profile_revision],
                    "source": [source.id, source.configuration_revision],
                }
            )
        )

    def _fetch_forms(self):
        self.ensure_one()
        # Check all user-visible boundaries before resolving protected credential
        # references. The runtime adapter keeps the token and App secret in memory.
        self._check_scope()
        try:
            adapter = MetaLeadFormsReadAdapter(
                self.webhook_page_id.sudo(),
                expected_page_revision=self.webhook_page_id.revision,
                expected_app_revision=self.webhook_page_id.app_id.revision,
            )
            return adapter.discover_lead_forms(self.webhook_page_id.external_page_id)
        except MetaApiError:
            # Provider errors or cursors must not enter UI messages or logs.
            raise UserError(
                _(
                    "Não foi possível consultar os formulários na Meta. "
                    "Confira o perfil de acesso e tente novamente."
                )
            ) from None

    def _action(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Descobrir formulários na Meta"),
            "res_model": self._name,
            "view_mode": "form",
            "res_id": self.id,
            "target": "new",
        }

    def action_discover(self):
        self.ensure_one()
        _check_admin(self)
        self.check_access_rights("write")
        self.check_access_rule("write")
        self._check_scope()
        fingerprint = self._fingerprint()
        forms = self._fetch_forms()
        routes = (
            self.env["marketing.center.meta.lead.route"]
            .with_context(active_test=False)
            .search([("webhook_page_id", "=", self.webhook_page_id.id)])
        )
        by_form = {route.external_form_id: route for route in routes}
        lines = [(5, 0, 0)]
        for form in forms:
            route = by_form.get(form.external_form_id)
            disposition = (
                "configured"
                if route
                else ("new" if form.status == "ACTIVE" else "archived")
            )
            lines.append(
                (
                    0,
                    0,
                    {
                        "external_form_id": form.external_form_id,
                        "name": form.name,
                        "form_status": form.status,
                        "reported_leads_count": form.leads_count,
                        "form_created_at": form.created_at,
                        "route_id": route.id if route else False,
                        "disposition": disposition,
                        "selected": False,
                    },
                )
            )
        self._internal().write(
            {
                "line_ids": lines,
                "discovered_at": fields.Datetime.now(),
                "scope_fingerprint": fingerprint,
                "state": "preview",
            }
        )
        return self._action()

    def action_configure(self):
        self.ensure_one()
        _check_admin(self)
        self.check_access_rights("write")
        self.check_access_rule("write")
        self._check_scope()
        if (
            self.state not in {"preview", "done"}
            or not self.discovered_at
            or fields.Datetime.now() - self.discovered_at > _MAX_PREVIEW_AGE
            or self.scope_fingerprint != self._fingerprint()
        ):
            raise UserError(_("Consulte os formulários novamente antes de configurar."))
        selected = self.line_ids.filtered("selected")
        if not selected:
            raise UserError(_("Selecione os formulários que deseja receber."))
        # Re-read from Meta before enabling a new route: a form might have been
        # archived, moved out of the Page or changed after the preview.
        current_forms = {form.external_form_id: form for form in self._fetch_forms()}
        model = self.env["marketing.center.meta.lead.route"].with_context(
            active_test=False
        )
        model.check_access_rights("create")
        created = model.browse()
        configured = model.browse()
        for line in selected.sorted("external_form_id"):
            acquire_advisory_xact_lock(
                self.env.cr,
                "marketing.meta.form.route:%s:%s"
                % (self.webhook_page_id.id, line.external_form_id),
            )
            route = model.search(
                [
                    ("webhook_page_id", "=", self.webhook_page_id.id),
                    ("external_form_id", "=", line.external_form_id),
                ],
                limit=1,
            )
            if not route:
                form = current_forms.get(line.external_form_id)
                if not form or form.status != "ACTIVE" or line.form_status != "ACTIVE":
                    raise UserError(
                        _(
                            "Somente formulários ativos podem receber uma nova configuração."
                        )
                    )
                if form.name != line.name:
                    raise UserError(
                        _("Um formulário mudou na Meta. Consulte a lista novamente.")
                    )
                values = {
                    "name": form.name,
                    "company_id": self.company_id.id,
                    "webhook_page_id": self.webhook_page_id.id,
                    "lead_profile_id": self.lead_profile_id.id,
                    "source_id": self.source_id.id,
                    "external_form_id": form.external_form_id,
                    "active": True,
                    "reconcile_enabled": True,
                    "initial_sync_period": self.initial_sync_period,
                }
                if "crm_auto_create_lead" in model._fields:
                    values["crm_auto_create_lead"] = False
                try:
                    with self.env.cr.savepoint():
                        route = model.create(values)
                except pg_errors.UniqueViolation as error:
                    if (
                        error.diag.constraint_name
                        != "marketing_center_meta_lead_route_page_form_unique"
                    ):
                        raise
                    raise MarketingSerializationFailure(
                        "Concurrent Meta form configuration requires a fresh snapshot"
                    ) from None
                created |= route
            # Existing routes, including archived ones and different CRM policies,
            # are only linked in the result; never reactivated or rewritten.
            configured |= route
            line._internal().write({"route_id": route.id, "disposition": "configured"})
        if created:
            # Reuse the selected Page's existing endpoint and consumer. This may
            # enqueue idempotent subscription reconciliation; no endpoint is made.
            created[:1].action_configure_webhook()
            if self.initial_sync_period != "new":
                for route in created:
                    route.action_enqueue_reconciliation()
        self._internal().write(
            {"state": "done", "scope_fingerprint": self._fingerprint()}
        )
        return {
            "type": "ir.actions.act_window",
            "name": _("Formulários configurados"),
            "res_model": model._name,
            "view_mode": "tree,form",
            "domain": [("id", "in", configured.ids)],
            "context": {"active_test": False},
            "target": "current",
        }


class MarketingCenterMetaLeadDiscoveryLine(models.TransientModel):
    _name = "marketing.center.meta.lead.discovery.line"
    _description = "Formulário encontrado na Meta"
    _order = "name, external_form_id, id"
    _check_company_auto = True

    discovery_id = fields.Many2one(
        "marketing.center.meta.lead.discovery",
        required=True,
        ondelete="cascade",
        readonly=True,
    )
    company_id = fields.Many2one(
        related="discovery_id.company_id", store=True, readonly=True
    )
    selected = fields.Boolean(string="Selecionar", default=False)
    external_form_id = fields.Char(
        string="ID na Meta", required=True, size=40, readonly=True
    )
    name = fields.Char(string="Formulário", required=True, size=512, readonly=True)
    form_status = fields.Char(
        string="Estado na Meta", required=True, size=32, readonly=True
    )
    reported_leads_count = fields.Integer(
        string="Total informado pela Meta", readonly=True
    )
    form_created_at = fields.Datetime(string="Formulário criado em", readonly=True)
    route_id = fields.Many2one(
        "marketing.center.meta.lead.route",
        string="Configuração existente",
        readonly=True,
        check_company=True,
    )
    disposition = fields.Selection(
        [
            ("new", "Novo"),
            ("configured", "Já configurado"),
            ("archived", "Arquivado ou indisponível"),
        ],
        string="Situação no Marketing Center",
        readonly=True,
        required=True,
    )

    def _internal(self):
        return self.with_context(**{_CONTEXT_KEY: _DISCOVERY_TOKEN})

    @api.model_create_multi
    def create(self, vals_list):
        _check_admin(self)
        if not _internal(self):
            raise AccessError(_("Os formulários são preenchidos pela consulta à Meta."))
        return super().create(vals_list)

    def write(self, values):
        _check_admin(self)
        if not _internal(self) and set(values) - {"selected"}:
            raise AccessError(_("Somente a seleção dos formulários pode ser alterada."))
        return super().write(values)

    def unlink(self):
        if not _internal(self) and not self.env.su:
            raise AccessError(
                _("Consulte os formulários novamente para atualizar a lista.")
            )
        return super().unlink()


class MarketingCenterMetaLeadRouteDiscovery(models.Model):
    _inherit = "marketing.center.meta.lead.route"

    def action_open_discovery(self):
        self.ensure_one()
        self._check_admin()
        self.check_access_rights("read")
        self.check_access_rule("read")
        return {
            "type": "ir.actions.act_window",
            "name": _("Descobrir formulários na Meta"),
            "res_model": "marketing.center.meta.lead.discovery",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_company_id": self.company_id.id,
                "default_webhook_page_id": self.webhook_page_id.id,
                "default_lead_profile_id": self.lead_profile_id.id,
                "default_source_id": self.source_id.id,
                "default_initial_sync_period": "new",
            },
        }

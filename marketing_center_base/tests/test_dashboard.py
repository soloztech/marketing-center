import datetime
import decimal
import json
import uuid

import pytz
from lxml import etree

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests.common import SavepointCase, tagged

from odoo.addons.marketing_center_base.models.attribution_resolution import (
    ASSET_RESOLUTION_WRITE_TOKEN,
)
from odoo.addons.marketing_center_base.services import (
    MarketingBusinessEventDTO,
    MarketingTouchpointDTO,
    capture_policy,
)
from odoo.addons.marketing_center_base.services.business_event_dto import (
    BUSINESS_EVENT_TYPES,
)
from odoo.addons.marketing_center_base.services.performance_dto import (
    MarketingPerformanceDTO,
)

from ..models import dashboard


@tagged("post_install", "-at_install")
class TestMarketingCenterDashboard(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.now = fields.Datetime.now()
        cls.today = cls.now.date()
        cls.source = cls._source("Visible Meta", "9001")
        cls.hidden_source = cls._source("Hidden Meta", "9002")
        cls.viewer = cls._user(
            "marketing_center_base.group_marketing_center_viewer", "viewer"
        )
        cls.analyst = cls._user(
            "marketing_center_base.group_marketing_center_analyst", "analyst"
        )
        cls.team = cls.env["marketing.center.team"].create(
            {"name": "Dashboard roster", "company_id": cls.company.id}
        )
        cls.env["marketing.center.team.member"].create(
            {
                "team_id": cls.team.id,
                "user_id": cls.viewer.id,
                "role": "viewer",
            }
        )
        cls.env["marketing.center.team.member"].create(
            {
                "team_id": cls.team.id,
                "user_id": cls.analyst.id,
                "role": "analyst",
            }
        )
        cls.env["marketing.center.team.source"].create(
            {
                "team_id": cls.team.id,
                "source_id": cls.source.id,
                "access_mode": "read",
            }
        )
        cls.account_metric, cls.account_run = cls._metric(
            cls.source, "account", "act_9001", clicks=10, cost=1_000_000
        )
        # The same provider day is intentionally available at campaign grain.
        # The dashboard must select one context instead of double-counting it.
        cls._metric(
            cls.source,
            "campaign",
            "act_9001/campaigns/42",
            clicks=500,
            cost=50_000_000,
        )
        cls._metric(
            cls.hidden_source,
            "account",
            "act_9002",
            clicks=99,
            cost=9_000_000,
        )
        cls._touchpoint(
            {"test.source_id": "9001"},
            resolved_source=cls.source,
        )
        cls._touchpoint({"other.unmapped_asset": "opaque"})
        cls._business_event("conversation_started", 1)
        cls._business_event("lead_created", 2)
        cls._revenue_event("invoice_posted", 3, "125.50")
        cls._revenue_event("payment_allocated", 4, "80.00")

    @classmethod
    def _source(cls, name, account_id):
        return cls.env["marketing.center.source"].create(
            {
                "name": name,
                "company_id": cls.company.id,
                "service": "meta.ads",
                "external_account_ref": "act_%s" % account_id,
                "external_account_id": account_id,
                "currency_id": cls.company.currency_id.id,
                "timezone": "UTC",
                "state": "active",
            }
        )

    @classmethod
    def _user(cls, group_xmlid, suffix):
        group = cls.env.ref(group_xmlid)
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Dashboard roster %s" % suffix,
                    "login": "dashboard-%s-%s" % (suffix, uuid.uuid4()),
                    "company_id": cls.company.id,
                    "company_ids": [Command.set(cls.company.ids)],
                    "groups_id": [Command.set(group.ids)],
                }
            )
        )

    @classmethod
    def _metric(cls, source, grain, external_ref, *, clicks, cost):
        period_start = datetime.datetime.combine(cls.today, datetime.time.min)
        period_end = period_start + datetime.timedelta(days=1)
        connection = cls.env["marketing.center.connection"].search(
            [("source_id", "=", source.id)], limit=1
        )
        if not connection:
            connection = cls.env["marketing.center.connection"].create(
                {
                    "name": "%s reader" % source.name,
                    "source_id": source.id,
                    "adapter_key": "test.dashboard",
                    "purpose": "reader",
                    "profile_public_ref": "dashboard:%s" % source.public_ref,
                    "profile_revision": 1,
                    "state": "ready",
                }
            )
        reporting_context = {
            "contract_version": "test.dashboard.v1",
            "grain": grain,
            "currency": source.currency_id.name,
            "report_timezone": source.timezone,
        }
        sync_service = cls.env["marketing.center.sync.service"]
        run = sync_service._plan_run(
            cls.company,
            source,
            connection,
            sync_kind="metrics",
            grain=grain,
            scope_ref="dashboard:%s:%s" % (source.public_ref, grain),
            trigger_kind="manual",
            trigger_ref=str(uuid.uuid4()),
            reporting_context=reporting_context,
            # Meta's normal Insights run covers seven closed days while every
            # metric remains a daily fact. Keep this fixture faithful so the
            # dashboard cannot accidentally require equal run/metric windows.
            window_start=period_start - datetime.timedelta(days=6),
            window_end=period_end,
            report_timezone=source.timezone,
        )
        sync_service._transition(run, "running", {"started_at": cls.now})
        result = cls.env["marketing.center.performance.service"]._upsert_metric(
            cls.company,
            source,
            MarketingPerformanceDTO(
                grain=grain,
                entity_external_ref=external_ref,
                report_date=cls.today,
                period_start_utc=period_start,
                period_end_utc=period_end,
                report_timezone="UTC",
                currency=source.currency_id.name,
                observed_at=cls.now,
                clicks=clicks,
                cost_micros=cost,
                reporting_context_hash=run.reporting_context_hash,
            ),
            sync_run=run,
        )
        sync_service._transition(
            run,
            "succeeded",
            {"finished_at": cls.now, "page_count": 1, "received_count": 1},
        )
        return cls.env["marketing.center.metric.daily"].browse(result.metric_id), run

    @classmethod
    def _touchpoint(cls, asset_refs, occurred_at=None, resolved_source=None):
        occurrence = str(uuid.uuid4())
        occurred_at = occurred_at or cls.now
        result = cls.env["marketing.attribution.service"]._ingest_touchpoint(
            cls.company,
            MarketingTouchpointDTO(
                source_system="test.dashboard",
                source_scope_ref="dashboard-suite",
                source_occurrence_ref=occurrence,
                source_evidence_ref="evidence:%s" % occurrence,
                occurred_at=occurred_at,
                observed_at=cls.now,
                platform="meta",
                channel="paid_social",
                network="meta",
                touchpoint_type="paid_ad_signal",
                evidence_level="provider_asserted",
                asset_refs=asset_refs,
            ),
        )
        if resolved_source:
            cls._project_source_resolution(result, resolved_source)
        return result

    @classmethod
    def _project_source_resolution(cls, result, source):
        """Materialize the provider-neutral resolver contract for this test."""

        resolution = (
            cls.env["marketing.attribution.asset.resolution"]
            .sudo()
            .search(
                [
                    ("company_id", "=", cls.company.id),
                    ("canonical_key", "=", result.canonical_key),
                ]
            )
        )
        resolution.ensure_one()
        resolution.with_context(
            marketing_asset_resolution_write_token=ASSET_RESOLUTION_WRITE_TOKEN
        ).write(
            {
                "target_kind": "source",
                "provider_key": "test",
                "service_key": "test.dashboard",
                "canonical_external_ref": source.external_account_ref,
                "source_id": source.id,
                "state": "resolved",
                "reason": "source_resolved",
                "source_candidate_count": 1,
                "resolved_at": cls.now,
            }
        )

    @classmethod
    def _business_event(cls, event_type, source_res_id):
        occurrence = str(uuid.uuid4())
        return cls.env["marketing.business.event.service"]._ingest_event(
            cls.company,
            MarketingBusinessEventDTO(
                event_class="lifecycle",
                event_type=event_type,
                source_system="test.dashboard",
                source_model="test.dashboard.origin",
                source_res_id=source_res_id,
                source_occurrence_ref=occurrence,
                source_evidence_ref=occurrence,
                business_event_key="dashboard:%s:%s" % (event_type, occurrence),
                occurred_at=cls.now,
                observed_at=cls.now,
                evidence_level="first_party",
            ),
        )

    @classmethod
    def _revenue_event(cls, event_type, source_res_id, amount):
        occurrence = str(uuid.uuid4())
        return cls.env["marketing.business.event.service"]._ingest_event(
            cls.company,
            MarketingBusinessEventDTO(
                event_class="revenue",
                event_type=event_type,
                source_system="test.dashboard",
                source_model="test.dashboard.origin",
                source_res_id=source_res_id,
                source_occurrence_ref=occurrence,
                source_evidence_ref=occurrence,
                business_event_key="dashboard:%s:%s" % (event_type, occurrence),
                occurred_at=cls.now,
                observed_at=cls.now,
                evidence_level="first_party",
                amount_signed=decimal.Decimal(amount),
                currency=cls.company.currency_id.name,
            ),
        )

    def _row(self, row_kind, source=None, user=None):
        model = self.env["marketing.center.dashboard.overview"]
        if user:
            model = model.with_user(user)
        domain = [
            ("company_id", "=", self.company.id),
            ("row_kind", "=", row_kind),
        ]
        if source:
            domain.append(("source_id", "=", source.id))
        return model.search(domain, limit=1)

    def test_dashboard_separates_source_resolution_and_company_facts(self):
        source_row = self._row("source", self.source)
        summary = self._row("summary")
        unresolved = self._row("unresolved")

        self.assertEqual(source_row.performance_grain, "account")
        self.assertTrue(source_row.has_clicks)
        self.assertEqual(source_row.clicks, 10)
        self.assertTrue(source_row.has_cost)
        self.assertEqual(source_row.cost_micros, 1_000_000)
        self.assertEqual(source_row.cost_amount, 1.0)
        self.assertEqual(source_row.touchpoint_count, 1)

        self.assertEqual(summary.total_touchpoint_count, 2)
        self.assertTrue(summary.has_touchpoint_data)
        self.assertEqual(summary.resolved_touchpoint_count, 1)
        self.assertEqual(summary.unresolved_touchpoint_count, 1)
        self.assertEqual(summary.resolution_coverage, 50.0)
        self.assertEqual(summary.conversation_started_count, 1)
        self.assertEqual(summary.lead_created_count, 1)
        self.assertEqual(summary.invoice_posted_count, 1)
        self.assertEqual(summary.payment_allocated_count, 1)
        self.assertEqual(summary.credit_note_posted_count, 0)
        self.assertEqual(summary.payment_allocation_reversed_count, 0)
        self.assertEqual(unresolved.touchpoint_count, 1)
        self.assertFalse(summary.source_id)
        self.assertFalse(unresolved.source_id)

    def test_response_counts_keep_conversation_and_episode_grains_separate(self):
        for channel_id in (101, 102):
            self._business_event("conversation_started", channel_id)
        for channel_id in (101, 101, 102):
            self._business_event("interaction_started", channel_id)
            self._business_event("first_human_response", channel_id)
            self._business_event("response_episode_answered", channel_id)
        # A conversation producer may identify another first-response message.
        # It must not create an additional answered episode in this projection.
        self._business_event("first_human_response", 101)

        summary = self._row("summary")

        # setUpClass already contributes one unrelated conversation start.
        self.assertEqual(summary.conversation_started_count, 3)
        self.assertEqual(summary.conversation_first_response_count, 2)
        self.assertEqual(summary.response_episode_started_count, 3)
        self.assertEqual(summary.response_episode_answered_count, 3)

    def test_summary_exposes_utc_and_transition_semantics(self):
        summary = self._row("summary")
        self.assertEqual(summary.timezone, "UTC")
        self.assertEqual(
            summary.window_start_date, self.today - datetime.timedelta(days=29)
        )
        self.assertEqual(summary.window_end_date, self.today)
        self._business_event("won", 123)
        self._business_event("won", 123)
        summary.invalidate_recordset()
        self.assertEqual(summary.won_count, 2)
        self.assertIn("transition", summary._fields["won_count"].string.lower())

    def test_dashboard_never_double_counts_lower_performance_grains(self):
        for grain, external_ref in (
            ("ad_group", "act_9001/ad_groups/43"),
            ("ad", "act_9001/ads/44"),
            ("keyword", "act_9001/keywords/45"),
        ):
            self._metric(
                self.source,
                grain,
                external_ref,
                clicks=700,
                cost=70_000_000,
            )
        source_row = self._row("source", self.source)
        self.assertEqual(source_row.performance_grain, "account")
        self.assertEqual(source_row.clicks, 10)
        self.assertEqual(source_row.cost_micros, 1_000_000)

    def test_source_touchpoint_window_uses_source_timezone(self):
        source_timezone = pytz.timezone("America/Sao_Paulo")
        local_today = pytz.utc.localize(self.now).astimezone(source_timezone).date()
        local_window_start = source_timezone.localize(
            datetime.datetime.combine(
                local_today - datetime.timedelta(days=29),
                datetime.time.min,
            )
        )
        window_start_utc = local_window_start.astimezone(pytz.utc).replace(tzinfo=None)
        self.source.write({"timezone": source_timezone.zone})
        self._touchpoint(
            {"test.source_id": "9001"},
            occurred_at=window_start_utc + datetime.timedelta(minutes=1),
            resolved_source=self.source,
        )
        self._touchpoint(
            {"test.source_id": "9001"},
            occurred_at=window_start_utc - datetime.timedelta(minutes=1),
            resolved_source=self.source,
        )

        source_row = self._row("source", self.source)

        # One fixture from setUpClass plus only the event inside the local window.
        self.assertEqual(source_row.touchpoint_count, 2)

    def test_portuguese_dashboard_translation_and_language_cache(self):
        self.env["res.lang"]._activate_lang("pt_BR")
        self.env["ir.module.module"].search(
            [("name", "=", "marketing_center_base")]
        )._update_translations("pt_BR", overwrite=True)
        summary = self._row("summary")
        self.assertEqual(
            summary.with_context(lang="en_US").display_name,
            "Company overview — %s" % self.company.name,
        )
        self.assertEqual(
            summary.with_context(lang="pt_BR").display_name,
            "Visão da empresa — %s" % self.company.name,
        )
        translated = summary.with_context(lang="pt_BR").fields_get(
            ["qualified_count", "timezone"]
        )
        self.assertEqual(
            translated["qualified_count"]["string"], "Transições de qualificação"
        )
        self.assertEqual(
            translated["timezone"]["string"], "Fuso horário dos relatórios"
        )
        policy = summary.with_context(lang="pt_BR").fields_get(
            ["general_capture_state", "crm_capture_state"]
        )
        for field_name in ("general_capture_state", "crm_capture_state"):
            labels = dict(policy[field_name]["selection"])
            self.assertEqual(
                labels,
                {
                    "enabled": "Ligada desde antes desta janela",
                    "enabled_in_window": "Ligada, verificada só dentro desta janela",
                    "enabled_unknown_start": "Ligada, início desconhecido",
                    "disabled": "Desativada",
                    "not_applicable": "Não se aplica",
                },
            )
        views = (
            self.env["marketing.center.dashboard.overview"]
            .with_context(lang="pt_BR")
            .get_views([(False, "kanban"), (False, "form")])["views"]
        )
        self.assertIn("não medido, a captura está desligada", views["kanban"]["arch"])
        self.assertIn("Conversas, vendas e fatos financeiros", views["kanban"]["arch"])
        self.assertIn("não garante cobertura completa", views["form"]["arch"])
        action = self.env.ref(
            "marketing_center_base.action_marketing_dashboard_overview"
        )
        self.assertEqual(action.with_context(lang="pt_BR").name, "Visão gerencial")

    def test_roster_hides_other_sources_and_company_aggregate_rows(self):
        rows = (
            self.env["marketing.center.dashboard.overview"]
            .with_user(self.viewer)
            .search([("company_id", "=", self.company.id)])
        )

        self.assertEqual(rows.filtered("source_id").source_id, self.source)
        self.assertEqual(set(rows.mapped("row_kind")), {"source"})
        self.assertNotIn(self.hidden_source, rows.mapped("source_id"))
        self.assertFalse(rows.filtered(lambda row: not row.source_id))
        with self.assertRaises(AccessError):
            self._row("source", self.hidden_source).with_user(
                self.viewer
            ).check_access_rule("read")

    def test_analyst_gets_company_summary_but_not_unresolved_detail(self):
        rows = (
            self.env["marketing.center.dashboard.overview"]
            .with_user(self.analyst)
            .search([("company_id", "=", self.company.id)])
        )
        self.assertEqual(set(rows.mapped("row_kind")), {"summary", "source"})
        self.assertEqual(rows.filtered("source_id").source_id, self.source)
        self.assertFalse(rows.filtered(lambda row: row.row_kind == "unresolved"))

    def test_view_is_read_only_and_drilldown_reuses_source_acl(self):
        source_row = self._row("source", self.source, user=self.viewer)
        action = source_row.action_open_performance()
        self.assertEqual(action["domain"], [("source_id", "=", self.source.id)])

        with self.assertRaises(AccessError):
            source_row.sudo().write({"name": "forbidden"})
        with self.assertRaises(AccessError):
            self.env["marketing.center.dashboard.overview"].sudo().create(
                {"name": "forbidden"}
            )
        with self.assertRaises(AccessError):
            source_row.sudo().unlink()

    def test_company_scope_never_exposes_an_unavailable_company(self):
        other_company = self.env["res.company"].create(
            {"name": "Dashboard other company %s" % uuid.uuid4()}
        )
        other_source = (
            self.env["marketing.center.source"]
            .sudo()
            .with_context(allowed_company_ids=[self.company.id, other_company.id])
            .create(
                {
                    "name": "Other company source",
                    "company_id": other_company.id,
                    "service": "meta.ads",
                    "external_account_ref": "act_9901",
                    "external_account_id": "9901",
                    "currency_id": other_company.currency_id.id,
                    "timezone": "UTC",
                    "state": "active",
                }
            )
        )
        foreign_row = (
            self.env["marketing.center.dashboard.overview"]
            .sudo()
            .with_context(allowed_company_ids=[self.company.id, other_company.id])
            .search(
                [
                    ("row_kind", "=", "source"),
                    ("source_id", "=", other_source.id),
                ]
            )
        )
        self.assertTrue(foreign_row)
        with self.assertRaises(AccessError):
            foreign_row.with_user(self.viewer).with_context(
                allowed_company_ids=[self.company.id]
            ).check_access_rule("read")

    def test_viewer_cannot_use_business_event_drilldown(self):
        summary = self._row("summary").with_user(self.viewer)
        with self.assertRaises(AccessError):
            summary.action_open_business_events()

    def test_partial_latest_sync_hides_totals_and_is_explicit(self):
        old_run = self.account_run
        service = self.env["marketing.center.sync.service"]
        partial = service._plan_run(
            self.company,
            self.source,
            old_run.connection_id,
            sync_kind="metrics",
            grain=old_run.grain,
            scope_ref=old_run.scope_ref,
            trigger_kind="retry",
            trigger_ref=str(uuid.uuid4()),
            reporting_context=old_run.reporting_context_json,
            window_start=old_run.window_start,
            window_end=old_run.window_end,
            report_timezone=old_run.report_timezone,
        )
        service._transition(partial, "running", {"started_at": self.now})
        service._transition(
            partial,
            "partial",
            {"finished_at": self.now, "page_count": 1, "received_count": 1},
        )
        # A newer successful run for a shifted window must not mask the
        # incomplete run that is still the latest one covering this daily fact.
        shifted = service._plan_run(
            self.company,
            self.source,
            old_run.connection_id,
            sync_kind="metrics",
            grain=old_run.grain,
            scope_ref=old_run.scope_ref,
            trigger_kind="retry",
            trigger_ref=str(uuid.uuid4()),
            reporting_context=old_run.reporting_context_json,
            window_start=old_run.window_start + datetime.timedelta(days=7),
            window_end=old_run.window_end + datetime.timedelta(days=7),
            report_timezone=old_run.report_timezone,
        )
        service._transition(shifted, "running", {"started_at": self.now})
        service._transition(
            shifted,
            "succeeded",
            {"finished_at": self.now, "page_count": 1, "received_count": 0},
        )

        row = self._row("source", self.source)
        self.assertEqual(row.freshness_state, "partial")
        self.assertFalse(row.has_performance_data)
        self.assertFalse(row.has_clicks)
        self.assertFalse(row.has_cost)
        self.assertEqual(row.clicks, 0)
        self.assertEqual(row.cost_micros, 0)

    def test_company_fact_window_is_closed_open_and_drilldown_matches(self):
        future = self.now + datetime.timedelta(days=1)
        occurrence = str(uuid.uuid4())
        self.env["marketing.business.event.service"]._ingest_event(
            self.company,
            MarketingBusinessEventDTO(
                event_class="lifecycle",
                event_type="lead_created",
                source_system="test.dashboard",
                source_model="test.dashboard.origin",
                source_res_id=999,
                source_occurrence_ref=occurrence,
                source_evidence_ref=occurrence,
                business_event_key="dashboard:future:%s" % occurrence,
                occurred_at=future,
                observed_at=self.now,
                evidence_level="first_party",
            ),
        )
        summary = self._row("summary").with_user(self.analyst)
        self.assertEqual(summary.lead_created_count, 1)
        action = summary.action_open_business_events()
        self.assertEqual(action["domain"][0], ("company_id", "=", self.company.id))
        self.assertEqual(action["domain"][1][0:2], ("occurred_at", ">="))
        self.assertEqual(action["domain"][2][0:2], ("occurred_at", "<"))

    def test_optional_sections_follow_installed_bridge_modules(self):
        summary = self._row("summary")
        module = self.env["ir.module.module"].sudo()
        expected = {
            "has_contact_center": bool(
                module.search_count(
                    [
                        ("name", "=", "marketing_center_contact_center"),
                        ("state", "=", "installed"),
                    ]
                )
            ),
            "has_crm": True,
            "has_sale": bool(
                module.search_count(
                    [
                        ("name", "=", "marketing_center_sale"),
                        ("state", "=", "installed"),
                    ]
                )
            ),
            "has_account": bool(
                module.search_count(
                    [
                        ("name", "=", "marketing_center_account"),
                        ("state", "=", "installed"),
                    ]
                )
            ),
        }
        for field_name, value in expected.items():
            self.assertEqual(summary[field_name], value)

    def _summary_now(self):
        self.env.flush_all()
        model = self.env["marketing.center.dashboard.overview"]
        model.invalidate_model()
        return self._row("summary")

    def _set_policy(self, flag, enabled, changed_at):
        stamp = flag.replace("_enabled", "_changed_at")
        self.company.write({flag: enabled})
        # Pin the stamp explicitly; production sets it only on a real change.
        self.company.with_context(
            marketing_capture_stamp_token=capture_policy.CAPTURE_STAMP_TOKEN
        ).write({stamp: changed_at})

    def test_capture_policies_are_stated_per_producer(self):
        has_crm = "marketing_crm_events_enabled" in self.company._fields
        long_ago = self.now - datetime.timedelta(days=60)
        self._business_event("conversation_started", 501)
        self._business_event("lead_created", 502)
        # Production configuration: general capture off, CRM capture on.
        self._set_policy("marketing_business_events_enabled", False, False)
        if has_crm:
            self._set_policy("marketing_crm_events_enabled", True, long_ago)
        summary = self._summary_now()
        self.assertEqual(summary.general_capture_state, "disabled")
        self.assertTrue(summary.cc_last_event_at)
        self.assertEqual(
            summary.crm_capture_state, "enabled" if has_crm else "not_applicable"
        )
        # Inverse: general capture enabled during the window, CRM capture off.
        self._set_policy("marketing_business_events_enabled", True, self.now)
        if has_crm:
            self._set_policy("marketing_crm_events_enabled", False, self.now)
        summary = self._summary_now()
        self.assertEqual(summary.general_capture_state, "enabled_in_window")
        self.assertEqual(summary.general_capture_changed_at, self.now)
        self.assertEqual(
            summary.crm_capture_state, "disabled" if has_crm else "not_applicable"
        )
        # Retained facts stay in the window after a pause and reactivation.
        self.assertGreaterEqual(summary.conversation_started_count, 1)
        self._set_policy("marketing_business_events_enabled", True, False)
        self.assertEqual(
            self._summary_now().general_capture_state, "enabled_unknown_start"
        )
        self._set_policy("marketing_business_events_enabled", True, long_ago)
        self.assertEqual(self._summary_now().general_capture_state, "enabled")
        for row_kind in ("unresolved", "source"):
            row = self._row(row_kind, self.source if row_kind == "source" else None)
            self.assertEqual(row.general_capture_state, "not_applicable")
            self.assertEqual(row.crm_capture_state, "not_applicable")

    def test_disabled_policy_hides_numbers_in_kanban_and_form(self):
        views = self.env["marketing.center.dashboard.overview"].get_views(
            [(False, "kanban"), (False, "form")]
        )["views"]
        kanban = etree.fromstring(views["kanban"]["arch"])
        for name, state in (
            ("conversation_started_count", "general_capture_state"),
            ("lead_created_count", "crm_capture_state"),
            ("proposal_sent_count", "general_capture_state"),
            ("invoice_posted_count", "general_capture_state"),
        ):
            with self.subTest(view="kanban", field=name):
                [node] = kanban.xpath("//templates//field[@name='%s']" % name)
                guards = [
                    ancestor.get("t-if", "")
                    for ancestor in node.iterancestors()
                    if ancestor.get("t-if")
                ]
                self.assertTrue(
                    any(
                        "%s.raw_value !== 'disabled'" % state in guard
                        for guard in guards
                    ),
                    guards,
                )
        form = etree.fromstring(views["form"]["arch"])
        for name, state in (
            ("conversation_started_count", "general_capture_state"),
            ("lead_created_count", "crm_capture_state"),
            ("proposal_sent_count", "general_capture_state"),
            ("invoice_posted_count", "general_capture_state"),
        ):
            with self.subTest(view="form", field=name):
                [node] = form.xpath("//field[@name='%s']" % name)
                group = node.getparent()
                self.assertEqual(group.tag, "group")
                modifiers = json.loads(group.get("modifiers") or "{}")
                self.assertIn([state, "=", "disabled"], modifiers.get("invisible", []))
        self.assertIn("not measured, capture is disabled", views["kanban"]["arch"])
        self.assertIn("Not measured", views["form"]["arch"])

    def test_general_policy_notice_does_not_depend_on_contact_center(self):
        views = self.env["marketing.center.dashboard.overview"].get_views(
            [(False, "kanban"), (False, "form")]
        )["views"]
        kanban = etree.fromstring(views["kanban"]["arch"])
        for state in ("enabled_in_window", "enabled_unknown_start"):
            with self.subTest(state=state):
                nodes = kanban.xpath(
                    "//templates//div[contains(@t-if, "
                    "\"general_capture_state.raw_value === '%s'\")]" % state
                )
                self.assertEqual(len(nodes), 1)
                guard = nodes[0].get("t-if")
                for producer in ("has_contact_center", "has_sale", "has_account"):
                    self.assertIn(producer, guard)
                # Not nested in a producer block: sales or accounting alone show it.
                self.assertFalse(
                    [
                        ancestor
                        for ancestor in nodes[0].iterancestors()
                        if "has_" in (ancestor.get("t-if") or "")
                    ]
                )
        for label in ("Conversations:", "CRM:", "Sales:", "Financial facts:"):
            self.assertIn(label, views["kanban"]["arch"])
        form = etree.fromstring(views["form"]["arch"])
        [notice] = form.xpath(
            "//sheet/div[contains(., 'does not guarantee complete coverage')]"
        )
        modifiers = json.loads(notice.get("modifiers") or "{}")
        self.assertEqual(modifiers.get("invisible"), [["row_kind", "!=", "summary"]])

    def test_last_recorded_fact_covers_every_producer_event_type(self):
        groups = (
            dashboard._CC_EVENT_TYPES,
            dashboard._CRM_EVENT_TYPES,
            dashboard._SALE_EVENT_TYPES,
            dashboard._ACCOUNT_EVENT_TYPES,
        )
        flattened = [event_type for group in groups for event_type in group]
        self.assertEqual(len(flattened), len(set(flattened)))
        self.assertEqual(set(flattened), set(BUSINESS_EVENT_TYPES))
        service = self.env["marketing.business.event.service"]
        # Later than any fixture or pre-existing fact, so the maxima are ours.
        early = self.now - datetime.timedelta(days=3)
        late = self.now + datetime.timedelta(hours=1)

        def ingest(event_type, event_class, occurred_at, res_id, **values):
            occurrence = str(uuid.uuid4())
            dto = MarketingBusinessEventDTO(
                event_class=event_class,
                event_type=event_type,
                source_system="test.dashboard",
                source_model="test.dashboard.origin",
                source_res_id=res_id,
                source_occurrence_ref=occurrence,
                source_evidence_ref=occurrence,
                business_event_key="dashboard:%s:%s" % (event_type, occurrence),
                occurred_at=occurred_at,
                observed_at=self.now,
                evidence_level="first_party",
                **values,
            )
            service._ingest_event(self.company, dto)
            return dto

        ingest("lead_created", "lifecycle", early, 701)
        ingest("lead_stage_changed", "lifecycle", late, 701)
        currency = self.company.currency_id.name
        invoice = ingest(
            "invoice_posted",
            "revenue",
            early,
            702,
            amount_signed=decimal.Decimal("100"),
            currency=currency,
        )
        ingest(
            "invoice_posting_reversed",
            "revenue",
            late,
            702,
            amount_signed=decimal.Decimal("-100"),
            currency=currency,
            reverses_business_event_key=invoice.business_event_key,
        )
        summary = self._summary_now()
        self.assertEqual(summary.crm_last_event_at, late)
        self.assertEqual(summary.account_last_event_at, late)

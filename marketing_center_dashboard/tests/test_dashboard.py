import datetime
import decimal
import uuid

import pytz

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests.common import SavepointCase

from odoo.addons.marketing_center_base.models.attribution_resolution import (
    ASSET_RESOLUTION_WRITE_TOKEN,
)
from odoo.addons.marketing_center_base.services import (
    MarketingBusinessEventDTO,
    MarketingTouchpointDTO,
)
from odoo.addons.marketing_center_base.services.performance_dto import (
    MarketingPerformanceDTO,
)


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

        summary = self._row("summary")

        # setUpClass already contributes one unrelated conversation start.
        self.assertEqual(summary.conversation_started_count, 3)
        self.assertEqual(summary.conversation_first_response_count, 2)
        self.assertEqual(summary.response_episode_started_count, 3)
        self.assertEqual(summary.response_episode_answered_count, 3)

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
            "has_crm": bool(
                module.search_count(
                    [
                        ("name", "=", "marketing_center_crm"),
                        ("state", "=", "installed"),
                    ]
                )
            ),
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

import datetime
import uuid
from unittest.mock import patch

import pytz
from lxml import etree

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests.common import SavepointCase
from odoo.tools.misc import format_amount, format_date
from odoo.tools.safe_eval import safe_eval

from ..services.catalog_dto import ExternalEntityDTO
from ..services.performance_dto import MarketingPerformanceDTO
from ..services.timezone import local_date_boundary_utc
from ..services.tokens import MARKETING_SYNC_WRITE_TOKEN


class TestMarketingCampaignBoard(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.currency = cls.company.currency_id
        cls.meta = cls._source("Board Meta", "meta.ads", "act_7001", "UTC")
        cls.google = cls._source(
            "Board Google", "google.ads", "customers/7002", "America/Sao_Paulo"
        )
        cls.hidden = cls._source("Board hidden", "meta.ads", "act_7003", "UTC")
        # Real Meta accounts report in their own time zone; local days do not
        # start at UTC midnight there.
        cls.meta_br = cls._source(
            "Board Meta BR", "meta.ads", "act_7004", "America/Sao_Paulo"
        )
        cls.viewer = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Board viewer",
                    "login": "board-viewer-%s" % uuid.uuid4(),
                    "company_id": cls.company.id,
                    "company_ids": [Command.set(cls.company.ids)],
                    "groups_id": [
                        Command.set(
                            cls.env.ref(
                                "marketing_center_base.group_marketing_center_viewer"
                            ).ids
                        )
                    ],
                }
            )
        )
        team = cls.env["marketing.center.team"].create(
            {"name": "Board roster", "company_id": cls.company.id}
        )
        cls.env["marketing.center.team.member"].create(
            {"team_id": team.id, "user_id": cls.viewer.id, "role": "viewer"}
        )
        for source in (cls.meta, cls.google, cls.meta_br):
            cls.env["marketing.center.team.source"].create(
                {"team_id": team.id, "source_id": source.id, "access_mode": "read"}
            )
        cls.board = cls.env["marketing.center.external.entity"]

    # -- fixtures -------------------------------------------------------------

    @classmethod
    def _source(cls, name, service, account_ref, timezone):
        return cls.env["marketing.center.source"].create(
            {
                "name": name,
                "company_id": cls.company.id,
                "service": service,
                "external_account_ref": account_ref,
                "currency_id": cls.currency.id,
                "timezone": timezone,
                "state": "active",
            }
        )

    @classmethod
    def _entity(cls, source, ref, *, entity_type="campaign", parent="", **values):
        dto = ExternalEntityDTO(
            entity_type=entity_type,
            external_ref="%s/%s" % (source.external_account_ref, ref),
            external_id=ref.rsplit("/", 1)[-1],
            name=values.pop("name", "Campaign %s" % ref),
            parent_entity_type=values.pop("parent_type", ""),
            parent_external_ref=(
                "%s/%s" % (source.external_account_ref, parent) if parent else ""
            ),
            observed_at=datetime.datetime(2026, 9, 20, 12),
            **values,
        )
        result = cls.env["marketing.center.catalog.service"]._upsert_entity(
            source.company_id, source, dto
        )
        return cls.env["marketing.center.external.entity"].browse(result.entity_id)

    def _days(self, source):
        """The 30 local days, computed independently of the code under test."""

        timezone = pytz.timezone(source.timezone)
        today = pytz.utc.localize(fields.Datetime.now()).astimezone(timezone).date()
        days = []
        for offset in range(29, -1, -1):
            day = today - datetime.timedelta(days=offset)
            days.append(
                (
                    day,
                    local_date_boundary_utc(day, source.timezone),
                    local_date_boundary_utc(
                        day + datetime.timedelta(days=1), source.timezone
                    ),
                )
            )
        return days

    def _connection(self, source):
        connection = self.env["marketing.center.connection"].search(
            [("source_id", "=", source.id)], limit=1
        )
        return connection or self.env["marketing.center.connection"].create(
            {
                "name": "%s reader" % source.name,
                "source_id": source.id,
                "adapter_key": "test.board",
                "purpose": "reader",
                "profile_public_ref": "board:%s" % source.public_ref,
                "profile_revision": 1,
                "state": "ready",
            }
        )

    def _run(self, source, first, last, *, context="v1"):
        """A running campaign metrics run covering local days first..last."""

        days = {day: (start, stop) for day, start, stop in self._days(source)}
        service = self.env["marketing.center.sync.service"]
        run = service._plan_run(
            self.company,
            source,
            self._connection(source),
            sync_kind="metrics",
            grain="campaign",
            scope_ref="board:%s" % uuid.uuid4(),
            trigger_kind="manual",
            trigger_ref=str(uuid.uuid4()),
            reporting_context={
                "contract_version": "test.board.%s" % context,
                "grain": "campaign",
                "currency": source.currency_id.name,
                "report_timezone": source.timezone,
            },
            window_start=days[first][0],
            window_end=days[last][1],
            report_timezone=source.timezone,
        )
        service._transition(run, "running", {"started_at": fields.Datetime.now()})
        return run

    def _pages(self, run, pages):
        # Stands for the service recording pages already applied to metrics.
        run.with_context(marketing_sync_write_token=MARKETING_SYNC_WRITE_TOKEN).write(
            {"page_count": pages}
        )

    def _finish(self, run, state, pages):
        if state == "running":
            self._pages(run, pages)
            return run
        self.env["marketing.center.sync.service"]._transition(
            run, state, {"page_count": pages, "finished_at": fields.Datetime.now()}
        )
        return run

    def _metric(self, source, campaign, day, run, *, dimensions=None, **counters):
        start, stop = next(
            (begin, end) for item, begin, end in self._days(source) if item == day
        )
        self.env["marketing.center.performance.service"]._upsert_metric(
            self.company,
            source,
            MarketingPerformanceDTO(
                grain="campaign",
                entity_external_ref=campaign.external_ref,
                report_date=day,
                period_start_utc=start,
                period_end_utc=stop,
                report_timezone=source.timezone,
                currency=source.currency_id.name,
                observed_at=fields.Datetime.now(),
                dimensions=dimensions,
                reporting_context_hash=run.reporting_context_hash,
                **counters,
            ),
            sync_run=run,
        )

    def _card(self, campaign, user=None):
        record = campaign.with_user(user) if user else campaign
        record.invalidate_recordset()
        return record

    def _money(self, micros):
        return format_amount(self.env, micros / 1_000_000, self.currency)

    # -- 1, 2: campaigns, status and platform --------------------------------

    def test_only_campaigns_with_provider_statuses_and_filters(self):
        active_meta = self._entity(self.meta, "campaigns/1", remote_status="active")
        enabled_google = self._entity(
            self.google, "campaigns/2", remote_status="enabled"
        )
        paused = self._entity(self.meta, "campaigns/3", remote_status="campaign_paused")
        archived = self._entity(self.meta, "campaigns/4", remote_status="archived")
        removed = self._entity(self.google, "campaigns/5", remote_status="removed")
        group = self._entity(
            self.meta,
            "adsets/11",
            entity_type="group",
            parent="campaigns/1",
            parent_type="campaign",
        )
        action = self.env.ref("marketing_center_base.action_marketing_campaign_board")
        search = etree.fromstring(
            self.board.get_views([(action.search_view_id.id, "search")])["views"][
                "search"
            ]["arch"]
        )
        # Scoped to this test's sources so pre-existing campaigns never interfere.
        base_domain = safe_eval(action.domain) + [
            ("source_id", "in", (self.meta | self.google).ids)
        ]

        def filtered(name):
            node = search.xpath("//filter[@name='%s']" % name)[0]
            return self.board.search(base_domain + safe_eval(node.get("domain")))

        self.assertNotIn(group, self.board.search(base_domain))
        self.assertEqual(filtered("filter_active"), active_meta | enabled_google)
        self.assertEqual(filtered("filter_paused"), paused)
        self.assertEqual(filtered("filter_closed"), archived | removed)
        self.assertIn("'search_default_filter_active': 1", action.context)
        labels = {
            record: (record.campaign_status_label, record.campaign_status_group)
            for record in active_meta | enabled_google | paused | archived | removed
        }
        self.assertEqual(labels[active_meta], ("Active", "active"))
        self.assertEqual(labels[enabled_google], ("Enabled", "active"))
        self.assertEqual(labels[paused], ("Campaign paused", "paused"))
        self.assertEqual(labels[archived], ("Archived", "closed"))
        self.assertEqual(labels[removed], ("Removed", "closed"))
        self.assertEqual(active_meta.campaign_platform_label, "Meta Ads")
        self.assertEqual(enabled_google.campaign_platform_label, "Google Ads")
        self.env["res.lang"]._activate_lang("pt_BR")
        self.env["ir.module.module"].search(
            [("name", "=", "marketing_center_base")]
        )._update_translations("pt_BR", overwrite=True)
        translated = active_meta.with_context(lang="pt_BR")
        translated.invalidate_recordset()
        self.assertEqual(translated.campaign_status_label, "Ativa")
        self.assertEqual(
            enabled_google.with_context(lang="pt_BR").campaign_status_label, "Ativa"
        )

    def test_platform_groups_and_kanban_render_for_a_viewer(self):
        self._entity(self.meta, "campaigns/21", remote_status="active")
        self._entity(self.google, "campaigns/22", remote_status="enabled")
        self._entity(self.hidden, "campaigns/23", remote_status="active")
        board = self.board.with_user(self.viewer)
        groups = board.read_group(
            [("entity_type", "=", "campaign")], ["source_service"], ["source_service"]
        )
        self.assertEqual(
            {
                group["source_service"]: group["source_service_count"]
                for group in groups
            },
            {"meta.ads": 1, "google.ads": 1},
        )
        view = board.get_views([(False, "kanban")])["views"]["kanban"]
        arch = etree.fromstring(view["arch"])
        names = {node.get("name") for node in arch.xpath("//field")}
        self.assertNotIn("attributes_json", names)
        records = board.search([("entity_type", "=", "campaign")])
        self.assertEqual(len(records), 2, "the hidden source's campaign is not listed")
        values = records.read([name for name in names if name in board._fields])
        self.assertEqual(len(values), 2)

    # -- 3, 3b: protected projection -------------------------------------------

    def test_viewer_reads_period_and_budget_but_never_the_raw_attributes(self):
        campaign = self._entity(
            self.meta,
            "campaigns/31",
            remote_status="active",
            attributes={
                "meta.start_time": "2026-09-01T12:00:00Z",
                "meta.stop_time": "2026-10-01T02:00:00Z",
                "meta.daily_budget": 5000,
                "meta.bid_amount": 12,
            },
        )
        card = self._card(campaign, self.viewer)
        self.assertEqual(
            card.campaign_period_label,
            "%s to %s"
            % (
                format_date(self.env, datetime.date(2026, 9, 1)),
                format_date(self.env, datetime.date(2026, 10, 1)),
            ),
        )
        scale = 10**self.currency.decimal_places
        self.assertEqual(
            card.campaign_budget_label,
            "%s per day" % format_amount(self.env, 5000 / scale, self.currency),
        )
        with self.assertRaises(AccessError):
            card.read(["attributes_json"])
        lifetime = self._entity(
            self.meta,
            "campaigns/32",
            attributes={"meta.lifetime_budget": 120000},
        )
        self.assertEqual(
            self._card(lifetime, self.viewer).campaign_budget_label,
            "%s in total" % format_amount(self.env, 120000 / scale, self.currency),
        )
        self.assertEqual(
            self._card(lifetime, self.viewer).campaign_period_label,
            "Period not informed",
        )
        hidden = self._entity(
            self.hidden,
            "campaigns/33",
            attributes={"meta.daily_budget": 100},
        )
        with self.assertRaises(AccessError):
            hidden.with_user(self.viewer).read(["campaign_budget_label"])
        # A computed label never leaks attributes of a record the user cannot read.
        self.assertEqual(
            hidden.with_user(self.viewer)._campaign_projection_attributes(), {}
        )

    def test_a_zero_budget_never_hides_the_other_budget(self):
        scale = 10**self.currency.decimal_places
        cases = [
            (
                {"meta.daily_budget": 0, "meta.lifetime_budget": 120000},
                "%s in total" % format_amount(self.env, 120000 / scale, self.currency),
            ),
            (
                {"meta.daily_budget": 5000, "meta.lifetime_budget": 0},
                "%s per day" % format_amount(self.env, 5000 / scale, self.currency),
            ),
            (
                {"meta.daily_budget": 0, "meta.lifetime_budget": 0},
                "Budget not informed",
            ),
        ]
        for index, (attributes, expected) in enumerate(cases):
            with self.subTest(attributes=attributes):
                campaign = self._entity(
                    self.meta, "campaigns/35%s" % index, attributes=attributes
                )
                self.assertEqual(
                    self._card(campaign, self.viewer).campaign_budget_label, expected
                )

    def test_google_periods_use_local_dates_and_report_missing_budget(self):
        cases = {
            "full": (
                {
                    "google.startDateTime": "2026-01-01 00:00:00",
                    "google.endDateTime": "2026-12-31 23:59:59",
                },
                "%s to %s"
                % (
                    format_date(self.env, datetime.date(2026, 1, 1)),
                    format_date(self.env, datetime.date(2026, 12, 31)),
                ),
            ),
            "utc": (
                # Stored as UTC: 02:00Z is still the previous day in São Paulo.
                {"google.startDateTime": "2026-03-10T02:00:00Z"},
                "Since %s" % format_date(self.env, datetime.date(2026, 3, 9)),
            ),
            "stop": (
                {"google.endDateTime": "2026-12-31"},
                "Until %s" % format_date(self.env, datetime.date(2026, 12, 31)),
            ),
            "missing": ({}, "Period not informed"),
            "malformed": (
                {"google.startDateTime": "31/12/2026", "google.endDateTime": "x"},
                "Period not informed",
            ),
            "one side malformed": (
                # Each key is judged on its own: the valid end still shows.
                {
                    "google.startDateTime": "31/12/2026",
                    "google.endDateTime": "2026-12-31 23:59:59",
                },
                "Until %s" % format_date(self.env, datetime.date(2026, 12, 31)),
            ),
        }
        for index, (label, (attributes, expected)) in enumerate(cases.items()):
            with self.subTest(case=label):
                campaign = self._entity(
                    self.google, "campaigns/4%s" % index, attributes=attributes
                )
                card = self._card(campaign, self.viewer)
                self.assertEqual(card.campaign_period_label, expected)
                self.assertEqual(card.campaign_budget_label, "Budget not synchronized")

    # -- 4, 4b, 4c: deterministic performance ------------------------------------

    def test_performance_counts_only_the_newest_covering_run(self):
        campaign = self._entity(self.meta_br, "campaigns/51", remote_status="active")
        other = self._entity(self.meta_br, "campaigns/52", remote_status="active")
        days = [day for day, _start, _stop in self._days(self.meta_br)]
        first, second = days[-2], days[-1]
        card = self._card(campaign)
        self.assertEqual(card.campaign_performance_state, "no_sync")

        old = self._run(self.meta_br, first, second, context="old")
        self._metric(self.meta_br, campaign, first, old, impressions=900, clicks=90)
        self._finish(old, "succeeded", 1)
        new = self._run(self.meta_br, first, second, context="new")
        self._metric(self.meta_br, campaign, first, new, impressions=100, clicks=10)
        self._metric(self.meta_br, campaign, second, new, impressions=50, clicks=5)
        # A breakdown row of the same campaign and day is never added.
        self._metric(
            self.meta_br,
            campaign,
            second,
            new,
            dimensions={"device": "mobile"},
            impressions=999,
            clicks=99,
        )
        self._finish(new, "succeeded", 1)
        card = self._card(campaign)
        self.assertEqual(card.campaign_performance_state, "measured")
        self.assertEqual(card.campaign_impressions_label, "150")
        self.assertEqual(card.campaign_clicks_label, "15")
        self.assertEqual(card.campaign_cost_label, "Not measured")
        self.assertEqual(card.campaign_performance_note, "Data for 2 of 30 days")

        # A successful refresh without rows for a campaign never reuses old rows.
        self.assertEqual(self._card(other).campaign_performance_state, "no_data")
        empty = self._run(self.meta_br, first, second, context="empty")
        self._finish(empty, "succeeded", 1)
        self.assertEqual(self._card(campaign).campaign_performance_state, "no_data")

        partial = self._run(self.meta_br, second, second, context="partial")
        self._metric(self.meta_br, campaign, second, partial, impressions=1)
        self._finish(partial, "partial", 1)
        card = self._card(campaign)
        self.assertEqual(card.campaign_performance_state, "incomplete")
        self.assertFalse(card.campaign_impressions_label)
        self.assertEqual(
            card.campaign_performance_note,
            "Affected days: %s to %s"
            % (format_date(self.env, second), format_date(self.env, second)),
        )

    def test_failures_shifted_windows_and_unfinished_pages_hide_totals(self):
        campaign = self._entity(self.meta_br, "campaigns/61", remote_status="active")
        days = [day for day, _start, _stop in self._days(self.meta_br)]

        def state():
            return self._card(campaign).campaign_performance_state

        # A first attempt that failed without any page leaves its days incomplete.
        self._finish(self._run(self.meta_br, days[-3], days[-1]), "failed", 0)
        self.assertEqual(state(), "incomplete")
        good = self._run(self.meta_br, days[-3], days[-1])
        for day in days[-3:]:
            self._metric(self.meta_br, campaign, day, good, clicks=4)
        self._finish(good, "succeeded", 1)
        self.assertEqual(state(), "measured")
        self.assertEqual(self._card(campaign).campaign_clicks_label, "12")
        # A later failure without pages over the same days hides the old totals.
        self._finish(self._run(self.meta_br, days[-3], days[-1]), "failed", 0)
        self.assertEqual(state(), "incomplete")
        # Shifted windows: 10–16 partial, then 17–23 succeeded.
        partial = self._run(self.meta_br, days[-14], days[-8])
        self._finish(partial, "partial", 1)
        recovered = self._run(self.meta_br, days[-7], days[-1])
        self._finish(recovered, "succeeded", 1)
        card = self._card(campaign)
        self.assertEqual(card.campaign_performance_state, "incomplete")
        self.assertEqual(
            card.campaign_performance_note,
            "Affected days: %s to %s"
            % (format_date(self.env, days[-14]), format_date(self.env, days[-8])),
        )
        cover = self._run(self.meta_br, days[-14], days[-1])
        for day in days[-14:]:
            self._metric(self.meta_br, campaign, day, cover, clicks=1)
        self._finish(cover, "succeeded", 3)
        self.assertEqual(state(), "measured")
        self.assertEqual(self._card(campaign).campaign_clicks_label, "14")
        # Running with two of three pages written: no half-written totals.
        running = self._run(self.meta_br, days[-14], days[-1])
        for day in days[-14:-7]:
            self._metric(self.meta_br, campaign, day, running, clicks=100)
        self._finish(running, "running", 2)
        self.assertEqual(state(), "updating")
        for day in days[-7:]:
            self._metric(self.meta_br, campaign, day, running, clicks=100)
        self._finish(running, "succeeded", 3)
        self.assertEqual(state(), "measured")
        self.assertEqual(self._card(campaign).campaign_clicks_label, "1,400")
        # Cancelled or stale after a page: incomplete until a covering success.
        for final in ("cancelled", "stale"):
            with self.subTest(final=final):
                broken = self._run(self.meta_br, days[-2], days[-1])
                self._metric(self.meta_br, campaign, days[-1], broken, clicks=7)
                self._pages(broken, 1)
                self._finish(broken, final, 1)
                self.assertEqual(state(), "incomplete")
                repair = self._run(self.meta_br, days[-14], days[-1])
                for day in days[-14:]:
                    self._metric(self.meta_br, campaign, day, repair, clicks=2)
                self._finish(repair, "succeeded", 1)
                self.assertEqual(state(), "measured")
        # A cancellation before any page never touched the metrics.
        idle = self._run(self.meta_br, days[-2], days[-1])
        self._finish(idle, "cancelled", 0)
        self.assertEqual(state(), "measured")
        self.assertEqual(self._card(campaign).campaign_clicks_label, "28")

    def test_each_indicator_states_its_own_measurement(self):
        campaign = self._entity(self.meta_br, "campaigns/71", remote_status="active")
        days = [day for day, _start, _stop in self._days(self.meta_br)]
        run = self._run(self.meta_br, days[-3], days[-1])
        self._metric(self.meta_br, campaign, days[-3], run, impressions=10, clicks=0)
        self._metric(self.meta_br, campaign, days[-2], run, impressions=20, clicks=0)
        self._metric(self.meta_br, campaign, days[-1], run, impressions=30)
        self._finish(run, "succeeded", 1)
        card = self._card(campaign)
        self.assertEqual(card.campaign_impressions_label, "60")
        self.assertEqual(
            card.campaign_clicks_label,
            "0 (measured on 2 of 3 days)",
            "measured zeros are shown as zero, with their coverage",
        )
        self.assertEqual(card.campaign_cost_label, "Not measured")
        costly = self._entity(self.meta_br, "campaigns/72", remote_status="active")
        again = self._run(self.meta_br, days[-3], days[-1])
        self._metric(self.meta_br, costly, days[-1], again, cost_micros=2_500_000)
        self._metric(self.meta_br, campaign, days[-3], again, impressions=1)
        self._metric(self.meta_br, campaign, days[-2], again, impressions=1)
        self._metric(self.meta_br, campaign, days[-1], again, impressions=1)
        self._finish(again, "succeeded", 1)
        self.assertEqual(
            self._card(costly).campaign_cost_label,
            "%s (measured on 1 of 3 days)" % self._money(2_500_000),
        )
        self.assertEqual(self._card(costly).campaign_impressions_label, "Not measured")
        self.assertEqual(self._card(costly).campaign_clicks_label, "Not measured")
        # Impressions only: clicks and cost are unmeasured, not zero.
        card = self._card(campaign)
        self.assertEqual(card.campaign_impressions_label, "3")
        self.assertEqual(card.campaign_clicks_label, "Not measured")
        self.assertEqual(card.campaign_cost_label, "Not measured")

    def test_a_time_zone_correction_never_reuses_other_day_bounds(self):
        source = self._source("Board moving", "meta.ads", "act_7006", "UTC")
        self.env["marketing.center.team.source"].create(
            {
                "team_id": self.env["marketing.center.team"]
                .search([("name", "=", "Board roster")], limit=1)
                .id,
                "source_id": source.id,
                "access_mode": "read",
            }
        )
        campaign = self._entity(source, "campaigns/91", remote_status="active")
        days = [day for day, _start, _stop in self._days(source)]
        utc_run = self._run(source, days[-3], days[-1])
        for day in days[-3:]:
            self._metric(source, campaign, day, utc_run, clicks=10)
        self._finish(utc_run, "succeeded", 1)
        self.assertEqual(self._card(campaign).campaign_clicks_label, "30")
        # The account time zone is corrected before any new synchronization.
        source.write({"timezone": "America/Sao_Paulo"})
        card = self._card(campaign)
        self.assertEqual(
            card.campaign_performance_state,
            "no_sync",
            "UTC days never certify São Paulo days",
        )
        # A partial backfill in the corrected time zone covers only its days.
        local_days = [day for day, _start, _stop in self._days(source)]
        backfill = self._run(source, local_days[-2], local_days[-1], context="local")
        for day in local_days[-2:]:
            self._metric(source, campaign, day, backfill, clicks=1)
        self._finish(backfill, "succeeded", 1)
        card = self._card(campaign)
        self.assertEqual(card.campaign_performance_state, "measured")
        self.assertEqual(card.campaign_clicks_label, "2")
        self.assertEqual(card.campaign_performance_note, "Data for 2 of 30 days")

    def test_a_currency_correction_never_reuses_other_amounts(self):
        source = self._source("Board exchange", "meta.ads", "act_7007", "UTC")
        self.env["marketing.center.team.source"].create(
            {
                "team_id": self.env["marketing.center.team"]
                .search([("name", "=", "Board roster")], limit=1)
                .id,
                "source_id": source.id,
                "access_mode": "read",
            }
        )
        other = (
            self.env["res.currency"]
            .with_context(active_test=False)
            .search([("id", "!=", self.currency.id)], limit=1)
        )
        other.active = True
        campaign = self._entity(source, "campaigns/92", remote_status="active")
        days = [day for day, _start, _stop in self._days(source)]
        # An older failed attempt and a successful sync in the first currency.
        self._finish(self._run(source, days[-12], days[-10]), "failed", 0)
        first = self._run(source, days[-3], days[-1])
        for day in days[-3:]:
            self._metric(source, campaign, day, first, clicks=10)
        self._finish(first, "succeeded", 1)
        self.assertEqual(self._card(campaign).campaign_performance_state, "incomplete")
        # The account currency is corrected before any new synchronization.
        source.write({"currency_id": other.id})
        for user in (None, self.viewer):
            self.assertEqual(
                self._card(campaign, user).campaign_performance_state,
                "no_sync",
                "runs in the old currency neither certify nor spoil any day",
            )
        # A partial backfill in the corrected currency covers only its days.
        backfill = self._run(source, days[-2], days[-1], context="exchange")
        for day in days[-2:]:
            self._metric(source, campaign, day, backfill, clicks=1)
        self._finish(backfill, "succeeded", 1)
        for user in (None, self.viewer):
            card = self._card(campaign, user)
            self.assertEqual(card.campaign_performance_state, "measured")
            self.assertEqual(card.campaign_clicks_label, "2")
            self.assertEqual(card.campaign_performance_note, "Data for 2 of 30 days")

    def test_day_bounds_follow_the_sync_boundaries_across_dst(self):
        # Midnight repeats in the Azores when the clock falls back on 25/10/2026.
        source = self._source("Board Azores", "meta.ads", "act_7005", "Atlantic/Azores")
        days = self.board._campaign_window_days(
            source, now=datetime.datetime(2026, 10, 27, 12)
        )
        for day, start, stop in days:
            with self.subTest(day=day):
                self.assertEqual(start, local_date_boundary_utc(day, "Atlantic/Azores"))
                self.assertEqual(
                    stop,
                    local_date_boundary_utc(
                        day + datetime.timedelta(days=1), "Atlantic/Azores"
                    ),
                )
        self.assertEqual(days[-1][0], datetime.date(2026, 10, 27))

    # -- children, preview and no provider requests ------------------------------

    def test_children_counts_and_no_external_request(self):
        campaign = self._entity(self.meta, "campaigns/81", remote_status="active")
        for index in range(2):
            self._entity(
                self.meta,
                "adsets/8%s" % index,
                entity_type="group",
                parent="campaigns/81",
                parent_type="campaign",
            )
        for index in range(3):
            self._entity(
                self.meta,
                "ads/80%s" % index,
                entity_type="ad",
                parent="adsets/80",
                parent_type="group",
            )

        def forbidden(*_args, **_kwargs):
            raise AssertionError("the campaign board must not call a provider")

        view = self.board.get_views([(False, "kanban")])["views"]["kanban"]
        names = [
            node.get("name")
            for node in etree.fromstring(view["arch"]).xpath("//field")
            if node.get("name") in self.board._fields
        ]
        with patch("requests.Session.request", forbidden), patch(
            "requests.request", forbidden
        ):
            card = self._card(campaign, self.viewer)
            values = card.read(sorted(set(names)))[0]
        self.assertEqual(values["campaign_group_count"], 2)
        self.assertEqual(values["campaign_ad_count"], 3)
        self.assertIn("Creative preview not synchronized.", view["arch"])

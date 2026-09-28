"""Campaign board: what is synchronized for each campaign, and what is not.

The cards read only the local catalog, sync runs and daily metrics; no provider
request is made. Missing data is stated explicitly and never shown as zero.
"""

import datetime
import re

import pytz

from odoo import _, api, fields, models
from odoo.tools.misc import format_amount, format_date, formatLang

from ..services.dto import canonical_json, sha256_text
from ..services.timezone import LocalDateBoundaryError, local_date_boundary_utc

WINDOW_DAYS = 30
# Stored remote statuses are the provider enums in lower case.
_ACTIVE_STATUSES = ("active", "enabled")
_PAUSED_STATUSES = ("paused", "campaign_paused", "adset_paused")
_CLOSED_STATUSES = ("archived", "deleted", "removed")
_GROUP_TYPES = ("group", "ad_group")
# Runs that wrote metric pages, or that ended without them; see _day_coverage.
_TERMINAL_RUN_STATES = ("succeeded", "partial", "failed")
_PAGED_RUN_STATES = ("running", "cancelled", "stale")
_ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
_LOCAL_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Daily rows without breakdown dimensions carry the digest of ``{}``.
_EMPTY_DIMENSION_HASH = sha256_text(canonical_json({}))


def _status_group(status):
    if status in _ACTIVE_STATUSES:
        return "active"
    if status in _PAUSED_STATUSES:
        return "paused"
    if status in _CLOSED_STATUSES:
        return "closed"
    return "other"


def _attribute_date(value, timezone):
    """Local calendar date of a catalog date attribute, or None when malformed."""

    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        if _ISO_UTC_RE.fullmatch(value):
            moment = datetime.datetime.fromisoformat(value[:-1]).replace(
                tzinfo=pytz.utc
            )
            return moment.astimezone(timezone).date()
        # Google Ads reports campaign dates in the account time zone.
        if _LOCAL_DATETIME_RE.fullmatch(value):
            return datetime.datetime.fromisoformat(value.replace(" ", "T")).date()
        if _DATE_RE.fullmatch(value):
            return datetime.date.fromisoformat(value)
    except ValueError:
        return None
    return None


def _minor_units(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class MarketingCenterCampaignBoard(models.Model):
    _inherit = "marketing.center.external.entity"

    source_service = fields.Char(
        related="source_id.service",
        store=True,
        index=True,
        string="Platform",
    )
    campaign_platform_label = fields.Char(
        compute="_compute_campaign_status", string="Platform name"
    )
    campaign_status_label = fields.Char(
        compute="_compute_campaign_status", string="Campaign status"
    )
    campaign_status_group = fields.Selection(
        [
            ("active", "Active"),
            ("paused", "Paused"),
            ("closed", "Closed"),
            ("other", "Other"),
        ],
        compute="_compute_campaign_status",
        string="Status group",
    )
    campaign_period_label = fields.Char(
        compute="_compute_campaign_projection", string="Period"
    )
    campaign_budget_label = fields.Char(
        compute="_compute_campaign_projection", string="Budget"
    )
    campaign_performance_state = fields.Selection(
        [
            ("no_sync", "No metric synchronization"),
            ("incomplete", "Incomplete synchronization"),
            ("updating", "Synchronization in progress"),
            ("no_data", "No campaign performance data"),
            ("measured", "Measured"),
        ],
        compute="_compute_campaign_performance",
        string="Performance state",
    )
    campaign_performance_note = fields.Char(
        compute="_compute_campaign_performance", string="Performance note"
    )
    campaign_impressions_label = fields.Char(
        compute="_compute_campaign_performance", string="Impressions"
    )
    campaign_clicks_label = fields.Char(
        compute="_compute_campaign_performance", string="Clicks"
    )
    campaign_cost_label = fields.Char(
        compute="_compute_campaign_performance", string="Cost"
    )
    campaign_group_count = fields.Integer(
        compute="_compute_campaign_children", string="Ad sets or ad groups"
    )
    campaign_ad_count = fields.Integer(
        compute="_compute_campaign_children", string="Ads"
    )

    # -- status -------------------------------------------------------------

    def _campaign_status_labels(self):
        # Methods, not module functions: _() reads the language from ``self``.
        return {
            "active": _("Active"),
            "enabled": _("Enabled"),
            "paused": _("Paused"),
            "campaign_paused": _("Campaign paused"),
            "adset_paused": _("Ad set paused"),
            "archived": _("Archived"),
            "deleted": _("Deleted"),
            "removed": _("Removed"),
            "in_process": _("In process"),
            "with_issues": _("With issues"),
            "pending_review": _("Pending review"),
            "disapproved": _("Disapproved"),
            "preapproved": _("Preapproved"),
            "pending_billing_info": _("Pending billing information"),
        }

    def _campaign_platform_labels(self):
        return {"meta.ads": _("Meta Ads"), "google.ads": _("Google Ads")}

    @api.depends("source_service", "remote_status")
    @api.depends_context("lang")
    def _compute_campaign_status(self):
        labels = self._campaign_status_labels()
        platforms = self._campaign_platform_labels()
        for entity in self:
            status = entity.remote_status or ""
            entity.campaign_status_label = labels.get(status) or (
                status.replace("_", " ").capitalize() if status else _("Unknown")
            )
            entity.campaign_status_group = _status_group(status)
            entity.campaign_platform_label = platforms.get(
                entity.source_service, entity.source_service or ""
            )

    # -- protected projection of approved attributes -------------------------

    def _campaign_projection_attributes(self):
        """Approved attribute values of readable campaigns, keyed by record id.

        ``attributes_json`` stays restricted to administrators. Access is checked
        on the records themselves before the privileged read, and only the
        approved keys leave this method.
        """

        if not self.check_access_rights("read", raise_exception=False):
            return {}
        readable = self._filter_access_rules_python("read")
        approved = (
            "meta.start_time",
            "meta.stop_time",
            "meta.daily_budget",
            "meta.lifetime_budget",
            "google.startDateTime",
            "google.endDateTime",
        )
        result = {}
        for entity in readable.sudo():
            attributes = entity.attributes_json
            attributes = attributes if isinstance(attributes, dict) else {}
            result[entity.id] = {key: attributes.get(key) for key in approved}
        return result

    def _campaign_period(self, attributes, timezone):
        if self.source_service == "google.ads":
            start_key, stop_key = "google.startDateTime", "google.endDateTime"
        else:
            start_key, stop_key = "meta.start_time", "meta.stop_time"
        start = _attribute_date(attributes.get(start_key), timezone)
        stop = _attribute_date(attributes.get(stop_key), timezone)
        if start and stop:
            return _("%(start)s to %(stop)s") % {
                "start": format_date(self.env, start),
                "stop": format_date(self.env, stop),
            }
        if start:
            return _("Since %s") % format_date(self.env, start)
        if stop:
            return _("Until %s") % format_date(self.env, stop)
        return _("Period not informed")

    def _campaign_budget(self, attributes, currency):
        if self.source_service == "google.ads":
            return _("Budget not synchronized")
        daily = _minor_units(attributes.get("meta.daily_budget"))
        lifetime = _minor_units(attributes.get("meta.lifetime_budget"))
        # Meta reports budgets in the currency's minor unit. A zero budget is not
        # set at this level, so it never hides the other one.
        scale = 10 ** (currency.decimal_places if currency else 2)
        if daily and currency:
            return _("%s per day") % format_amount(self.env, daily / scale, currency)
        if lifetime and currency:
            return _("%s in total") % format_amount(
                self.env, lifetime / scale, currency
            )
        return _("Budget not informed")

    @api.depends("source_service", "source_id.timezone", "source_id.currency_id")
    @api.depends_context("lang", "uid")
    def _compute_campaign_projection(self):
        projected = self._campaign_projection_attributes()
        for entity in self:
            attributes = projected.get(entity.id)
            if attributes is None:
                entity.campaign_period_label = _("Period not informed")
                entity.campaign_budget_label = _("Budget not informed")
                continue
            timezone = entity._campaign_timezone()
            entity.campaign_period_label = entity._campaign_period(attributes, timezone)
            entity.campaign_budget_label = entity._campaign_budget(
                attributes, entity.source_id.currency_id
            )

    def _campaign_timezone(self):
        try:
            return pytz.timezone(self.source_id.timezone or "UTC")
        except pytz.UnknownTimeZoneError:
            return pytz.utc

    # -- children -----------------------------------------------------------

    @api.depends_context("uid")
    def _compute_campaign_children(self):
        entities = self.env["marketing.center.external.entity"]
        campaigns = self.filtered("id")
        groups = entities.search_read(
            [("parent_id", "in", campaigns.ids), ("entity_type", "in", _GROUP_TYPES)],
            ["parent_id"],
        )
        group_campaign = {row["id"]: row["parent_id"][0] for row in groups}
        ads = entities.search_read(
            [
                ("parent_id", "in", list(group_campaign)),
                ("entity_type", "=", "ad"),
            ],
            ["parent_id"],
        )
        group_counts = {}
        ad_counts = {}
        for campaign_id in group_campaign.values():
            group_counts[campaign_id] = group_counts.get(campaign_id, 0) + 1
        for row in ads:
            campaign_id = group_campaign[row["parent_id"][0]]
            ad_counts[campaign_id] = ad_counts.get(campaign_id, 0) + 1
        for entity in self:
            entity.campaign_group_count = group_counts.get(entity.id, 0)
            entity.campaign_ad_count = ad_counts.get(entity.id, 0)

    # -- performance ----------------------------------------------------------

    @api.model
    def _campaign_window_days(self, source, now=None):
        """The 30 local days of a source, each with its UTC bounds.

        Bounds come from the same helper as sync windows and metric periods, so a
        day whose midnight repeats or is skipped by a DST change matches them.
        """

        timezone_name = source.timezone or "UTC"
        try:
            timezone = pytz.timezone(timezone_name)
        except pytz.UnknownTimeZoneError:
            timezone_name, timezone = "UTC", pytz.utc
        now = now or fields.Datetime.now()
        today = pytz.utc.localize(now).astimezone(timezone).date()
        days = []
        for offset in range(WINDOW_DAYS - 1, -1, -1):
            day = today - datetime.timedelta(days=offset)
            try:
                start = local_date_boundary_utc(day, timezone_name)
                stop = local_date_boundary_utc(
                    day + datetime.timedelta(days=1), timezone_name
                )
            except LocalDateBoundaryError:
                start = datetime.datetime.combine(day, datetime.time())
                stop = start + datetime.timedelta(days=1)
            days.append((day, start, stop))
        return days

    @api.model
    def _campaign_metric_runs(self, sources, first_start, last_stop):
        """Campaign metric runs that decide each day; newest first.

        Terminal runs count even without pages (a failed attempt leaves its days
        incomplete). Running, cancelled and stale runs count once they wrote a
        page, because their pages stay applied. Runs without pages that have not
        finished never touched the metrics and are ignored.
        """

        runs = self.env["marketing.center.sync.run"].search(
            [
                ("source_id", "in", sources.ids),
                ("sync_kind", "=", "metrics"),
                ("grain", "=", "campaign"),
                ("window_start", "!=", False),
                ("window_end", "!=", False),
                ("window_start", "<", last_stop),
                ("window_end", ">", first_start),
                "|",
                ("state", "in", _TERMINAL_RUN_STATES),
                "&",
                ("state", "in", _PAGED_RUN_STATES),
                ("page_count", ">", 0),
            ],
            order="id desc",
        )
        by_source = {}
        for run in runs:
            by_source.setdefault(run.source_id.id, []).append(run)
        return by_source, self._campaign_run_currencies(runs)

    @api.model
    def _campaign_run_currencies(self, runs):
        """The currency each run reported in, from its admin-only context.

        Only runs already found under the user's record rules are projected, and
        only their currency code leaves the protected reporting context.
        """

        return {
            run.id: str(
                (run.reporting_context_json or {}).get("currency") or ""
            ).upper()
            for run in runs.sudo()
        }

    @api.model
    def _day_coverage(self, days, runs, timezone_name, currency_code, run_currencies):
        """Map each day to its deciding run (or None) and its coverage state.

        Only runs reported in the source's current time zone and currency decide
        its local days: after a correction, older runs have other day bounds or
        amounts, so they neither certify nor spoil the corrected days.
        """

        runs = [
            run
            for run in runs
            if run.report_timezone == timezone_name
            and currency_code
            and run_currencies.get(run.id) == currency_code
        ]
        coverage = {}
        for day, start, stop in days:
            deciding = next(
                (
                    run
                    for run in runs
                    if run.window_start <= start and run.window_end >= stop
                ),
                None,
            )
            if not deciding:
                state = "uncovered"
            elif deciding.state == "succeeded":
                state = "complete"
            elif deciding.state == "running":
                state = "updating"
            else:
                state = "incomplete"
            coverage[day] = (deciding, state)
        return coverage

    def _campaign_metric_rows(self, campaigns, first_day, last_day):
        """Daily campaign rows of the displayed campaigns only, without breakdowns."""

        return self.env["marketing.center.metric.daily"].search_read(
            [
                ("source_id", "in", campaigns.source_id.ids),
                ("grain", "=", "campaign"),
                ("metric_origin", "=", "platform_reported"),
                ("dimension_hash", "=", _EMPTY_DIMENSION_HASH),
                ("report_date", ">=", first_day),
                ("report_date", "<=", last_day),
                "|",
                ("entity_id", "in", campaigns.ids),
                ("entity_external_ref", "in", campaigns.mapped("external_ref")),
            ],
            [
                "source_id",
                "entity_id",
                "entity_external_ref",
                "report_date",
                "period_start_utc",
                "period_end_utc",
                "report_timezone",
                "currency",
                "last_sync_run_id",
                "has_impressions",
                "impressions",
                "has_clicks",
                "clicks",
                "has_cost_micros",
                "cost_micros",
            ],
            order="id",
        )

    def _performance_indicator(self, rows, flag, value, complete_days, formatter):
        measured = [row for row in rows if row[flag]]
        if not measured:
            return _("Not measured")
        label = formatter(sum(row[value] for row in measured))
        measured_days = len({row["report_date"] for row in measured})
        if measured_days < complete_days:
            label = _("%(value)s (measured on %(measured)s of %(days)s days)") % {
                "value": label,
                "measured": measured_days,
                "days": complete_days,
            }
        return label

    @api.depends_context("lang", "uid")
    def _compute_campaign_performance(self):
        empty = {
            "campaign_performance_state": "no_sync",
            "campaign_performance_note": False,
            "campaign_impressions_label": False,
            "campaign_clicks_label": False,
            "campaign_cost_label": False,
        }
        campaigns = self.filtered(lambda entity: entity.id and entity.source_id)
        for entity in self - campaigns:
            entity.update(empty)
        if not campaigns:
            return
        sources = campaigns.source_id
        windows = {source.id: self._campaign_window_days(source) for source in sources}
        first_start = min(days[0][1] for days in windows.values())
        last_stop = max(days[-1][2] for days in windows.values())
        runs, run_currencies = self._campaign_metric_runs(
            sources, first_start, last_stop
        )
        rows = self._campaign_metric_rows(
            campaigns,
            min(days[0][0] for days in windows.values()),
            max(days[-1][0] for days in windows.values()),
        )
        by_campaign = {}
        index = {(c.source_id.id, c.external_ref): c.id for c in campaigns}
        for row in rows:
            source_id = row["source_id"][0]
            campaign_id = (row["entity_id"] and row["entity_id"][0]) or index.get(
                (source_id, row["entity_external_ref"])
            )
            if campaign_id:
                by_campaign.setdefault(campaign_id, []).append(row)
        for source in sources:
            days = windows[source.id]
            timezone_name = source.timezone or "UTC"
            currency = source.currency_id
            coverage = self._day_coverage(
                days,
                runs.get(source.id, []),
                timezone_name,
                currency.name.upper() if currency else "",
                run_currencies,
            )
            bounds = {day: (start, stop) for day, start, stop in days}
            states = {state for _run, state in coverage.values()}
            incomplete = sorted(
                day for day, (_run, state) in coverage.items() if state == "incomplete"
            )
            complete = {
                day for day, (_run, state) in coverage.items() if state == "complete"
            }
            for entity in campaigns.filtered(
                lambda item, s=source: item.source_id == s
            ):
                values = dict(empty)
                if states == {"uncovered"}:
                    values["campaign_performance_state"] = "no_sync"
                elif incomplete:
                    values["campaign_performance_state"] = "incomplete"
                    values["campaign_performance_note"] = _(
                        "Affected days: %(start)s to %(stop)s"
                    ) % {
                        "start": format_date(self.env, incomplete[0]),
                        "stop": format_date(self.env, incomplete[-1]),
                    }
                elif "updating" in states:
                    values["campaign_performance_state"] = "updating"
                else:
                    selected = [
                        row
                        for row in by_campaign.get(entity.id, [])
                        if entity._selected_metric_row(
                            row, coverage, bounds, currency, timezone_name
                        )
                    ]
                    if not selected:
                        values["campaign_performance_state"] = "no_data"
                    else:
                        values.update(
                            entity._measured_performance(
                                selected, len(complete), currency
                            )
                        )
                    if len(complete) < WINDOW_DAYS:
                        values["campaign_performance_note"] = _(
                            "Data for %(days)s of %(window)s days"
                        ) % {"days": len(complete), "window": WINDOW_DAYS}
                entity.update(values)

    def _selected_metric_row(self, row, coverage, bounds, currency, timezone_name):
        """A row counts only if the run deciding its day wrote it successfully,
        for exactly that local day of the source's current time zone."""

        deciding, state = coverage.get(row["report_date"], (None, "uncovered"))
        start, stop = bounds.get(row["report_date"], (None, None))
        return bool(
            state == "complete"
            and row["last_sync_run_id"]
            and row["last_sync_run_id"][0] == deciding.id
            and row["period_start_utc"] == start
            and row["period_end_utc"] == stop
            and row["report_timezone"] == timezone_name
            and currency
            and row["currency"] == currency.name
        )

    def _measured_performance(self, rows, complete_days, currency):
        def count(value):
            return formatLang(self.env, value, digits=0)

        def cost(value):
            return format_amount(self.env, value / 1_000_000, currency)

        return {
            "campaign_performance_state": "measured",
            "campaign_impressions_label": self._performance_indicator(
                rows, "has_impressions", "impressions", complete_days, count
            ),
            "campaign_clicks_label": self._performance_indicator(
                rows, "has_clicks", "clicks", complete_days, count
            ),
            "campaign_cost_label": self._performance_indicator(
                rows, "has_cost_micros", "cost_micros", complete_days, cost
            ),
        }

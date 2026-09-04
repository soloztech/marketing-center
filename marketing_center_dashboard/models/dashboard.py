import datetime

from odoo import _, api, fields, models, tools
from odoo.exceptions import AccessError


class MarketingCenterDashboardOverview(models.Model):
    """Live, read-only overview over canonical Marketing Center projections.

    The view deliberately keeps three different meanings apart:

    * provider-reported performance belongs to one roster-scoped source;
    * effective touchpoints are counted once and may or may not resolve to a source;
    * Odoo lifecycle facts are company totals, never causal credit for a source.

    No raw identifier or provider payload is projected here.
    """

    _name = "marketing.center.dashboard.overview"
    _description = "Marketing Center Operational Overview"
    _auto = False
    _table = "marketing_center_dashboard_overview"
    _order = "row_rank, name, id"
    _rec_name = "name"
    _check_company_auto = True

    name = fields.Char(readonly=True)
    row_kind = fields.Selection(
        [
            ("summary", "Company facts (no causal credit)"),
            ("unresolved", "No unique source"),
            ("source", "Marketing source"),
        ],
        readonly=True,
        index=True,
    )
    row_rank = fields.Integer(readonly=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True)
    source_id = fields.Many2one(
        "marketing.center.source",
        readonly=True,
        ondelete="restrict",
        check_company=True,
    )
    source_state = fields.Selection(
        [
            ("draft", "Draft"),
            ("active", "Active"),
            ("paused", "Paused"),
            ("attention", "Attention"),
            ("disabled", "Disabled"),
        ],
        readonly=True,
    )
    service = fields.Char(readonly=True)
    timezone = fields.Char(readonly=True)
    currency_id = fields.Many2one("res.currency", readonly=True)
    metric_origin = fields.Selection(
        [("platform_reported", "Platform reported")], readonly=True
    )
    performance_grain = fields.Selection(
        [
            ("account", "Account"),
            ("campaign", "Campaign"),
            ("ad_group", "Ad group"),
            ("ad", "Ad"),
            ("keyword", "Keyword"),
            ("mixed", "Mixed daily basis"),
        ],
        readonly=True,
    )
    window_start_date = fields.Date(readonly=True)
    window_end_date = fields.Date(readonly=True)
    updated_at = fields.Datetime(readonly=True)
    performance_updated_at = fields.Datetime(readonly=True)
    freshness_state = fields.Selection(
        [
            ("fresh", "Fresh"),
            ("partial", "Incomplete synchronization"),
            ("delayed", "Delayed"),
            ("stale", "Stale"),
            ("no_data", "No performance data"),
            ("paused", "Read disabled"),
            ("not_applicable", "Not applicable"),
        ],
        readonly=True,
        index=True,
    )
    freshness_hours = fields.Float(readonly=True, digits=(16, 1))
    has_performance_data = fields.Boolean(readonly=True)
    has_clicks = fields.Boolean(readonly=True)
    clicks = fields.Integer(readonly=True, group_operator=False)
    has_cost = fields.Boolean(readonly=True)
    cost_micros = fields.Integer(readonly=True, group_operator=False)
    cost_amount = fields.Monetary(
        compute="_compute_cost_amount",
        currency_field="currency_id",
        readonly=True,
        group_operator=False,
    )
    has_touchpoint_data = fields.Boolean(readonly=True)
    touchpoint_count = fields.Integer(readonly=True, group_operator=False)
    total_touchpoint_count = fields.Integer(readonly=True, group_operator=False)
    resolved_touchpoint_count = fields.Integer(readonly=True, group_operator=False)
    unresolved_touchpoint_count = fields.Integer(readonly=True, group_operator=False)
    resolution_coverage = fields.Float(
        readonly=True,
        digits=(16, 2),
        group_operator=False,
        help=(
            "Share of effective touchpoints in the 30-day window that resolve to "
            "one unique marketing source. This is technical coverage, not causal "
            "attribution."
        ),
    )
    has_contact_center = fields.Boolean(readonly=True)
    has_crm = fields.Boolean(readonly=True)
    has_sale = fields.Boolean(readonly=True)
    has_account = fields.Boolean(readonly=True)
    conversation_started_count = fields.Integer(readonly=True, group_operator=False)
    conversation_first_response_count = fields.Integer(
        string="Conversations with first human response",
        readonly=True,
        group_operator=False,
        help=(
            "Number of distinct conversations with at least one confirmed human "
            "response in the dashboard window."
        ),
    )
    response_episode_started_count = fields.Integer(
        string="Response episodes started",
        readonly=True,
        group_operator=False,
        help=("Number of inbound response episodes opened in the dashboard window."),
    )
    response_episode_answered_count = fields.Integer(
        string="Response episodes answered",
        readonly=True,
        group_operator=False,
        help=(
            "Number of response episodes with a confirmed first human response "
            "in the dashboard window."
        ),
    )
    lead_created_count = fields.Integer(readonly=True, group_operator=False)
    qualified_count = fields.Integer(readonly=True, group_operator=False)
    won_count = fields.Integer(readonly=True, group_operator=False)
    lost_count = fields.Integer(readonly=True, group_operator=False)
    proposal_sent_count = fields.Integer(readonly=True, group_operator=False)
    order_confirmed_count = fields.Integer(readonly=True, group_operator=False)
    order_cancelled_count = fields.Integer(readonly=True, group_operator=False)
    invoice_posted_count = fields.Integer(readonly=True, group_operator=False)
    credit_note_posted_count = fields.Integer(readonly=True, group_operator=False)
    payment_allocated_count = fields.Integer(readonly=True, group_operator=False)
    payment_allocation_reversed_count = fields.Integer(
        readonly=True, group_operator=False
    )

    @api.depends("has_cost", "cost_micros")
    def _compute_cost_amount(self):
        for record in self:
            record.cost_amount = (
                record.cost_micros / 1_000_000 if record.has_cost else 0.0
            )

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(
            """
            CREATE VIEW marketing_center_dashboard_overview AS
            WITH dashboard_window AS (
                SELECT
                    (
                        (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date - 29
                    )::timestamp AS window_start,
                    (
                        (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date + 1
                    )::timestamp AS window_end
            ),
            source_scope AS (
                SELECT
                    source.id,
                    source.company_id,
                    source.name,
                    source.service,
                    source.state,
                    source.read_enabled,
                    source.timezone,
                    source.currency_id,
                    currency.name AS currency,
                    timezone(source.timezone, CURRENT_TIMESTAMP)::date AS local_date,
                    timezone(
                        'UTC',
                        timezone(
                            source.timezone,
                            (
                                timezone(source.timezone, CURRENT_TIMESTAMP)::date - 29
                            )::timestamp
                        )
                    ) AS window_start_utc,
                    timezone(
                        'UTC',
                        timezone(
                            source.timezone,
                            (
                                timezone(source.timezone, CURRENT_TIMESTAMP)::date + 1
                            )::timestamp
                        )
                    ) AS window_end_utc
                FROM marketing_center_source AS source
                JOIN res_currency AS currency ON currency.id = source.currency_id
                WHERE source.active IS TRUE
            ),
            metric_candidate AS (
                SELECT
                    metric.*,
                    latest_run.id AS applicable_run_id,
                    latest_run.state AS applicable_run_state
                FROM marketing_center_metric_daily AS metric
                JOIN source_scope AS source ON source.id = metric.source_id
                LEFT JOIN LATERAL (
                    SELECT run.id, run.state
                    FROM marketing_center_sync_run AS run
                    WHERE run.source_id = metric.source_id
                      AND run.sync_kind = 'metrics'
                      AND run.grain = metric.grain
                      AND run.reporting_context_hash =
                          metric.reporting_context_hash
                      AND run.window_start <= metric.period_start_utc
                      AND run.window_end >= metric.period_end_utc
                      AND (run.state = 'succeeded' OR run.page_count > 0)
                    ORDER BY run.id DESC
                    LIMIT 1
                ) AS latest_run ON TRUE
                WHERE metric.report_date BETWEEN source.local_date - 29
                                             AND source.local_date
                  AND metric.currency = source.currency
                  AND metric.metric_origin = 'platform_reported'
                  AND (
                      metric.dimensions_json IS NULL
                      OR metric.dimensions_json = '{}'::jsonb
                  )
            ),
            metric_run_quality AS (
                SELECT
                    source_id,
                    BOOL_OR(
                        applicable_run_id IS NOT NULL
                        AND (
                            applicable_run_state != 'succeeded'
                            OR last_sync_run_id IS DISTINCT FROM applicable_run_id
                        )
                    ) AS has_incomplete_sync
                FROM metric_candidate
                GROUP BY source_id
            ),
            metric_context AS (
                SELECT
                    metric.source_id,
                    metric.report_date,
                    metric.grain,
                    metric.reporting_context_hash,
                    BOOL_OR(metric.has_clicks) AS has_clicks,
                    SUM(CASE WHEN metric.has_clicks THEN metric.clicks ELSE 0 END)
                        AS clicks,
                    BOOL_OR(metric.has_cost_micros) AS has_cost,
                    SUM(CASE WHEN metric.has_cost_micros THEN metric.cost_micros ELSE 0 END)
                        AS cost_micros,
                    MAX(metric.last_observed_at) AS observed_at
                FROM metric_candidate AS metric
                WHERE (
                      (
                          metric.applicable_run_id IS NULL
                          AND metric.last_sync_run_id IS NULL
                      )
                      OR (
                          metric.applicable_run_state = 'succeeded'
                          AND metric.last_sync_run_id = metric.applicable_run_id
                      )
                  )
                GROUP BY
                    metric.source_id,
                    metric.report_date,
                    metric.grain,
                    metric.reporting_context_hash
            ),
            metric_context_ranked AS (
                SELECT
                    context.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY context.source_id, context.report_date
                        ORDER BY
                            ((context.has_clicks::integer)
                             + (context.has_cost::integer)) DESC,
                            CASE context.grain
                                WHEN 'account' THEN 0
                                WHEN 'campaign' THEN 1
                                WHEN 'ad_group' THEN 2
                                WHEN 'ad' THEN 3
                                WHEN 'keyword' THEN 4
                                ELSE 5
                            END,
                            context.observed_at DESC,
                            context.reporting_context_hash DESC
                    ) AS context_rank
                FROM metric_context AS context
            ),
            metric_selected AS (
                SELECT *
                FROM metric_context_ranked
                WHERE context_rank = 1
            ),
            metric_stats AS (
                SELECT
                    source_id,
                    MIN(report_date) AS window_start_date,
                    MAX(report_date) AS window_end_date,
                    BOOL_OR(has_clicks) AS has_clicks,
                    SUM(CASE WHEN has_clicks THEN clicks ELSE 0 END) AS clicks,
                    BOOL_OR(has_cost) AS has_cost,
                    SUM(CASE WHEN has_cost THEN cost_micros ELSE 0 END) AS cost_micros,
                    CASE
                        WHEN COUNT(DISTINCT grain) = 1 THEN MIN(grain)
                        ELSE 'mixed'
                    END AS performance_grain,
                    MAX(observed_at) AS observed_at
                FROM metric_selected
                GROUP BY source_id
            ),
            effective_recent AS (
                SELECT effective.*
                FROM marketing_attribution_effective_touchpoint AS effective
                CROSS JOIN dashboard_window AS bounds
                WHERE effective.occurred_at >=
                        bounds.window_start - INTERVAL '1 day'
                  AND effective.occurred_at <
                        bounds.window_end + INTERVAL '1 day'
            ),
            resolution_rollup AS (
                SELECT
                    effective.id AS touchpoint_id,
                    effective.company_id,
                    effective.occurred_at,
                    effective.observed_at,
                    COUNT(DISTINCT resolution.source_id)
                        FILTER (WHERE resolution.source_id IS NOT NULL)
                        AS source_count,
                    MIN(resolution.source_id)
                        FILTER (WHERE resolution.source_id IS NOT NULL)
                        AS candidate_source_id,
                    COALESCE(
                        BOOL_OR(resolution.state = 'ambiguous'),
                        FALSE
                    ) AS has_ambiguity
                FROM effective_recent AS effective
                LEFT JOIN marketing_attribution_asset_resolution AS resolution
                  ON resolution.touchpoint_id = effective.id
                 AND resolution.company_id = effective.company_id
                 AND resolution.canonical_key = effective.canonical_key
                GROUP BY
                    effective.id,
                    effective.company_id,
                    effective.occurred_at,
                    effective.observed_at
            ),
            classified_touchpoint AS (
                SELECT
                    touchpoint_id,
                    company_id,
                    occurred_at,
                    observed_at,
                    CASE
                        WHEN source_count = 1 AND has_ambiguity IS FALSE
                        THEN candidate_source_id
                        ELSE NULL
                    END AS source_id
                FROM resolution_rollup
            ),
            touchpoint_source_stats AS (
                SELECT
                    classified.company_id,
                    classified.source_id,
                    COUNT(*) AS touchpoint_count,
                    MAX(classified.observed_at) AS observed_at
                FROM classified_touchpoint AS classified
                JOIN source_scope AS source
                  ON source.id = classified.source_id
                 AND source.company_id = classified.company_id
                WHERE classified.source_id IS NOT NULL
                  AND classified.occurred_at >= source.window_start_utc
                  AND classified.occurred_at < source.window_end_utc
                GROUP BY classified.company_id, classified.source_id
            ),
            touchpoint_company_stats AS (
                SELECT
                    company_id,
                    COUNT(*) AS total_count,
                    COUNT(*) FILTER (WHERE source_id IS NOT NULL) AS resolved_count,
                    COUNT(*) FILTER (WHERE source_id IS NULL) AS unresolved_count,
                    MAX(observed_at) AS observed_at,
                    MAX(observed_at) FILTER (WHERE source_id IS NULL)
                        AS unresolved_observed_at
                FROM classified_touchpoint
                CROSS JOIN dashboard_window AS bounds
                WHERE occurred_at >= bounds.window_start
                  AND occurred_at < bounds.window_end
                GROUP BY company_id
            ),
            event_stats AS (
                SELECT
                    company_id,
                    COUNT(*) FILTER (WHERE event_type = 'conversation_started')
                        AS conversation_started_count,
                    COUNT(
                        DISTINCT (source_system, source_model, source_res_id)
                    ) FILTER (WHERE event_type = 'first_human_response')
                        AS conversation_first_response_count,
                    COUNT(*) FILTER (WHERE event_type = 'interaction_started')
                        AS response_episode_started_count,
                    COUNT(*) FILTER (WHERE event_type = 'first_human_response')
                        AS response_episode_answered_count,
                    COUNT(*) FILTER (WHERE event_type = 'lead_created')
                        AS lead_created_count,
                    COUNT(*) FILTER (WHERE event_type = 'qualified')
                        AS qualified_count,
                    COUNT(*) FILTER (WHERE event_type = 'won') AS won_count,
                    COUNT(*) FILTER (WHERE event_type = 'lost') AS lost_count,
                    COUNT(*) FILTER (WHERE event_type = 'proposal_sent')
                        AS proposal_sent_count,
                    COUNT(*) FILTER (WHERE event_type = 'order_confirmed')
                        AS order_confirmed_count,
                    COUNT(*) FILTER (WHERE event_type = 'order_cancelled')
                        AS order_cancelled_count,
                    COUNT(*) FILTER (WHERE event_type = 'invoice_posted')
                        AS invoice_posted_count,
                    COUNT(*) FILTER (WHERE event_type = 'credit_note_posted')
                        AS credit_note_posted_count,
                    COUNT(*) FILTER (WHERE event_type = 'payment_allocated')
                        AS payment_allocated_count,
                    COUNT(*) FILTER (
                        WHERE event_type = 'payment_allocation_reversed'
                    ) AS payment_allocation_reversed_count,
                    MAX(observed_at) AS observed_at
                FROM marketing_business_event
                CROSS JOIN dashboard_window AS bounds
                WHERE occurred_at >= bounds.window_start
                  AND occurred_at < bounds.window_end
                GROUP BY company_id
            ),
            module_flags AS (
                SELECT
                    EXISTS (
                        SELECT 1 FROM ir_module_module
                        WHERE name = 'marketing_center_contact_center'
                          AND state = 'installed'
                    ) AS has_contact_center,
                    EXISTS (
                        SELECT 1 FROM ir_module_module
                        WHERE name = 'marketing_center_crm'
                          AND state = 'installed'
                    ) AS has_crm,
                    EXISTS (
                        SELECT 1 FROM ir_module_module
                        WHERE name = 'marketing_center_sale'
                          AND state = 'installed'
                    ) AS has_sale,
                    EXISTS (
                        SELECT 1 FROM ir_module_module
                        WHERE name = 'marketing_center_account'
                          AND state = 'installed'
                    ) AS has_account
            ),
            company_scope AS (
                SELECT company.id, company.name
                FROM res_company AS company
                WHERE company.active IS TRUE
            ),
            source_rows AS (
                SELECT
                    source.id AS id,
                    source.name,
                    'source'::varchar AS row_kind,
                    20 AS row_rank,
                    source.company_id,
                    source.id AS source_id,
                    source.state AS source_state,
                    source.service,
                    source.timezone,
                    source.currency_id,
                    'platform_reported'::varchar AS metric_origin,
                    metric.performance_grain,
                    COALESCE(metric.window_start_date, source.local_date - 29)
                        AS window_start_date,
                    COALESCE(metric.window_end_date, source.local_date)
                        AS window_end_date,
                    GREATEST(metric.observed_at, source_touchpoint.observed_at)
                        AS updated_at,
                    metric.observed_at AS performance_updated_at,
                    CASE
                        WHEN source.read_enabled IS FALSE
                          OR source.state IN ('paused', 'disabled') THEN 'paused'
                        WHEN COALESCE(quality.has_incomplete_sync, FALSE)
                            THEN 'partial'
                        WHEN metric.observed_at IS NULL THEN 'no_data'
                        WHEN metric.observed_at >=
                            (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - INTERVAL '36 hours'
                            THEN 'fresh'
                        WHEN metric.observed_at >=
                            (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - INTERVAL '72 hours'
                            THEN 'delayed'
                        ELSE 'stale'
                    END::varchar AS freshness_state,
                    CASE
                        WHEN metric.observed_at IS NULL THEN NULL
                        ELSE GREATEST(
                            EXTRACT(EPOCH FROM (
                                (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')
                                - metric.observed_at
                            )) / 3600.0,
                            0.0
                        )
                    END AS freshness_hours,
                    (metric.source_id IS NOT NULL) AS has_performance_data,
                    COALESCE(metric.has_clicks, FALSE) AS has_clicks,
                    COALESCE(metric.clicks, 0) AS clicks,
                    COALESCE(metric.has_cost, FALSE) AS has_cost,
                    COALESCE(metric.cost_micros, 0) AS cost_micros,
                    (COALESCE(source_touchpoint.touchpoint_count, 0) > 0)
                        AS has_touchpoint_data,
                    COALESCE(source_touchpoint.touchpoint_count, 0)
                        AS touchpoint_count,
                    COALESCE(source_touchpoint.touchpoint_count, 0)
                        AS total_touchpoint_count,
                    COALESCE(source_touchpoint.touchpoint_count, 0)
                        AS resolved_touchpoint_count,
                    0::bigint AS unresolved_touchpoint_count,
                    CASE
                        WHEN COALESCE(source_touchpoint.touchpoint_count, 0) = 0
                            THEN 0.0
                        ELSE 100.0
                    END AS resolution_coverage,
                    flags.has_contact_center,
                    flags.has_crm,
                    flags.has_sale,
                    flags.has_account,
                    0::bigint AS conversation_started_count,
                    0::bigint AS conversation_first_response_count,
                    0::bigint AS response_episode_started_count,
                    0::bigint AS response_episode_answered_count,
                    0::bigint AS lead_created_count,
                    0::bigint AS qualified_count,
                    0::bigint AS won_count,
                    0::bigint AS lost_count,
                    0::bigint AS proposal_sent_count,
                    0::bigint AS order_confirmed_count,
                    0::bigint AS order_cancelled_count,
                    0::bigint AS invoice_posted_count,
                    0::bigint AS credit_note_posted_count,
                    0::bigint AS payment_allocated_count,
                    0::bigint AS payment_allocation_reversed_count
                FROM source_scope AS source
                CROSS JOIN module_flags AS flags
                LEFT JOIN metric_run_quality AS quality
                  ON quality.source_id = source.id
                LEFT JOIN metric_stats AS metric
                  ON metric.source_id = source.id
                 AND COALESCE(quality.has_incomplete_sync, FALSE) IS FALSE
                LEFT JOIN touchpoint_source_stats AS source_touchpoint
                  ON source_touchpoint.company_id = source.company_id
                 AND source_touchpoint.source_id = source.id
            ),
            summary_rows AS (
                SELECT
                    -(company.id * 2)::integer AS id,
                    ('Panorama ' || company.name)::varchar AS name,
                    'summary'::varchar AS row_kind,
                    0 AS row_rank,
                    company.id AS company_id,
                    NULL::integer AS source_id,
                    NULL::varchar AS source_state,
                    NULL::varchar AS service,
                    'UTC'::varchar AS timezone,
                    NULL::integer AS currency_id,
                    NULL::varchar AS metric_origin,
                    NULL::varchar AS performance_grain,
                    bounds.window_start::date AS window_start_date,
                    (bounds.window_end - INTERVAL '1 day')::date
                        AS window_end_date,
                    GREATEST(touchpoint.observed_at, event.observed_at) AS updated_at,
                    NULL::timestamp AS performance_updated_at,
                    'not_applicable'::varchar AS freshness_state,
                    NULL::numeric AS freshness_hours,
                    FALSE AS has_performance_data,
                    FALSE AS has_clicks,
                    0::bigint AS clicks,
                    FALSE AS has_cost,
                    0::bigint AS cost_micros,
                    (COALESCE(touchpoint.total_count, 0) > 0)
                        AS has_touchpoint_data,
                    COALESCE(touchpoint.total_count, 0) AS touchpoint_count,
                    COALESCE(touchpoint.total_count, 0) AS total_touchpoint_count,
                    COALESCE(touchpoint.resolved_count, 0)
                        AS resolved_touchpoint_count,
                    COALESCE(touchpoint.unresolved_count, 0)
                        AS unresolved_touchpoint_count,
                    CASE
                        WHEN COALESCE(touchpoint.total_count, 0) = 0 THEN 0.0
                        ELSE 100.0 * touchpoint.resolved_count / touchpoint.total_count
                    END AS resolution_coverage,
                    flags.has_contact_center,
                    flags.has_crm,
                    flags.has_sale,
                    flags.has_account,
                    COALESCE(event.conversation_started_count, 0)
                        AS conversation_started_count,
                    COALESCE(event.conversation_first_response_count, 0)
                        AS conversation_first_response_count,
                    COALESCE(event.response_episode_started_count, 0)
                        AS response_episode_started_count,
                    COALESCE(event.response_episode_answered_count, 0)
                        AS response_episode_answered_count,
                    COALESCE(event.lead_created_count, 0) AS lead_created_count,
                    COALESCE(event.qualified_count, 0) AS qualified_count,
                    COALESCE(event.won_count, 0) AS won_count,
                    COALESCE(event.lost_count, 0) AS lost_count,
                    COALESCE(event.proposal_sent_count, 0) AS proposal_sent_count,
                    COALESCE(event.order_confirmed_count, 0)
                        AS order_confirmed_count,
                    COALESCE(event.order_cancelled_count, 0)
                        AS order_cancelled_count,
                    COALESCE(event.invoice_posted_count, 0)
                        AS invoice_posted_count,
                    COALESCE(event.credit_note_posted_count, 0)
                        AS credit_note_posted_count,
                    COALESCE(event.payment_allocated_count, 0)
                        AS payment_allocated_count,
                    COALESCE(event.payment_allocation_reversed_count, 0)
                        AS payment_allocation_reversed_count
                FROM company_scope AS company
                CROSS JOIN module_flags AS flags
                CROSS JOIN dashboard_window AS bounds
                LEFT JOIN touchpoint_company_stats AS touchpoint
                  ON touchpoint.company_id = company.id
                LEFT JOIN event_stats AS event ON event.company_id = company.id
            ),
            unresolved_rows AS (
                SELECT
                    -(company.id * 2 + 1)::integer AS id,
                    'Não atribuível — sem fonte única'::varchar AS name,
                    'unresolved'::varchar AS row_kind,
                    10 AS row_rank,
                    company.id AS company_id,
                    NULL::integer AS source_id,
                    NULL::varchar AS source_state,
                    NULL::varchar AS service,
                    'UTC'::varchar AS timezone,
                    NULL::integer AS currency_id,
                    NULL::varchar AS metric_origin,
                    NULL::varchar AS performance_grain,
                    bounds.window_start::date AS window_start_date,
                    (bounds.window_end - INTERVAL '1 day')::date
                        AS window_end_date,
                    touchpoint.unresolved_observed_at AS updated_at,
                    NULL::timestamp AS performance_updated_at,
                    'not_applicable'::varchar AS freshness_state,
                    NULL::numeric AS freshness_hours,
                    FALSE AS has_performance_data,
                    FALSE AS has_clicks,
                    0::bigint AS clicks,
                    FALSE AS has_cost,
                    0::bigint AS cost_micros,
                    (COALESCE(touchpoint.total_count, 0) > 0)
                        AS has_touchpoint_data,
                    COALESCE(touchpoint.unresolved_count, 0) AS touchpoint_count,
                    COALESCE(touchpoint.total_count, 0) AS total_touchpoint_count,
                    COALESCE(touchpoint.resolved_count, 0)
                        AS resolved_touchpoint_count,
                    COALESCE(touchpoint.unresolved_count, 0)
                        AS unresolved_touchpoint_count,
                    CASE
                        WHEN COALESCE(touchpoint.total_count, 0) = 0 THEN 0.0
                        ELSE 100.0 * touchpoint.resolved_count / touchpoint.total_count
                    END AS resolution_coverage,
                    flags.has_contact_center,
                    flags.has_crm,
                    flags.has_sale,
                    flags.has_account,
                    0::bigint AS conversation_started_count,
                    0::bigint AS conversation_first_response_count,
                    0::bigint AS response_episode_started_count,
                    0::bigint AS response_episode_answered_count,
                    0::bigint AS lead_created_count,
                    0::bigint AS qualified_count,
                    0::bigint AS won_count,
                    0::bigint AS lost_count,
                    0::bigint AS proposal_sent_count,
                    0::bigint AS order_confirmed_count,
                    0::bigint AS order_cancelled_count,
                    0::bigint AS invoice_posted_count,
                    0::bigint AS credit_note_posted_count,
                    0::bigint AS payment_allocated_count,
                    0::bigint AS payment_allocation_reversed_count
                FROM company_scope AS company
                CROSS JOIN module_flags AS flags
                CROSS JOIN dashboard_window AS bounds
                LEFT JOIN touchpoint_company_stats AS touchpoint
                  ON touchpoint.company_id = company.id
            )
            SELECT * FROM summary_rows
            UNION ALL
            SELECT * FROM unresolved_rows
            UNION ALL
            SELECT * FROM source_rows
            """
        )

    @api.model_create_multi
    def create(self, vals_list):  # pylint: disable=method-required-super
        del vals_list
        raise AccessError(_("The marketing dashboard is read-only."))

    def write(self, values):  # pylint: disable=method-required-super
        del values
        raise AccessError(_("The marketing dashboard is read-only."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("The marketing dashboard is read-only."))

    def action_open_performance(self):
        self.ensure_one()
        if self.row_kind != "source" or not self.source_id:
            return False
        self.source_id.check_access_rule("read")
        action = self.env["ir.actions.actions"]._for_xml_id(
            "marketing_center_base.action_marketing_metric_daily"
        )
        action["domain"] = [("source_id", "=", self.source_id.id)]
        action["context"] = {
            "search_default_group_date": 1,
            "search_default_group_grain": 1,
        }
        return action

    def action_open_business_events(self):
        self.ensure_one()
        if self.row_kind != "summary" or not self.user_has_groups(
            "marketing_center_base.group_marketing_center_analyst"
        ):
            raise AccessError(_("Only Marketing Analysts can inspect business events."))
        action = self.env["ir.actions.actions"]._for_xml_id(
            "marketing_center_base.action_marketing_business_events"
        )
        action["domain"] = [
            ("company_id", "=", self.company_id.id),
            ("occurred_at", ">=", fields.Datetime.to_string(self.window_start_date)),
            (
                "occurred_at",
                "<",
                fields.Datetime.to_string(
                    self.window_end_date + datetime.timedelta(days=1)
                ),
            ),
        ]
        return action

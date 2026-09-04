Marketing Center Dashboard
==========================

Read-only Odoo 16 overview for the first operational management slice.

The dashboard deliberately separates:

* platform-reported cost and clicks, scoped by source roster;
* effective touchpoints resolved to exactly one source;
* an explicit line for touchpoints without a unique source;
* company lifecycle facts emitted by optional Contact Center, CRM and Sales bridges.

Lifecycle totals are correlation and operational facts. They are never displayed as
causal attribution to a source, campaign or ad. Optional-domain sections appear only
when their bridge addon is installed; this addon never imports optional domain models.

The reporting window is the latest 30 days. For each source and local report date the
view selects one reporting context, preferring the most complete context and account
grain on ties. Only dimensionless facts enter this first overview. This prevents
account and campaign grains or provider breakdowns from being summed twice.

Source cards follow the user's active source roster. Company lifecycle aggregates are
available to Marketing Analysts, while the unresolved aggregate remains administrator
only because unresolved touchpoints are not yet safe to associate with a roster.

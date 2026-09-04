Marketing Center - Meta Lead Ads CRM
====================================

Optional glue between authenticated Meta Lead Ads submissions and Odoo CRM.

The route is disabled by default.  When explicitly enabled, one durable queue job
creates at most one ``crm.lead`` for each submission already fetched from the Meta
Graph API.  A webhook hint, form ID, campaign ID or ad ID can never create a lead.

Only a fixed allow-list is projected: personal name, email, telephone and company
name.  Arbitrary form answers remain in the private Lead Ads field ledger and are
never copied into the CRM description, chatter or marketing evidence.

The submission's canonical touchpoint is linked to the resulting lead through the
append-only ``meta.lead_submission`` assertion authority.  Replays return the same
projection, lead and assertion.  The addon does not create ``res.partner`` records
and does not make either the Meta connector or CRM depend on each other.

Installation and upgrades enqueue one bootstrap job per company.  Each job scans
only a bounded, ID-ordered submission page and atomically chains the next cursor,
so historical convergence is resumable and never turns module installation into
an unbounded Lead Ads scan.

CRM owns the projected lead lifecycle. Deleting that lead clears only the live
navigation reference; the Meta projection and assertion retain the original
model, record ID, company and bounded display reference for audit. No deleted
lead is recreated by retry or exposed by an action.

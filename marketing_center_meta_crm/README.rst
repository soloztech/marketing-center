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

Configuration
-------------

Enable automatic CRM creation on the Lead Ads route.  New routes default to
``Follow CRM settings``: a selected sales team's ``use_leads`` setting determines
whether to create a lead or an opportunity.  Without a selected team, the global
CRM Leads setting determines the type.  Explicit ``Lead`` and ``Opportunity``
choices remain available.

Sales team and salesperson are optional and have no automatic defaults.  Leave
both empty to create unassigned records.  The resulting lead or opportunity is a
native ``crm.lead``, linked to the submission's existing canonical touchpoint.

New routes default to ``From a date``.  On first activation, an empty cutoff is
initialized automatically.  The date is editable and is retained when the route
is disabled and enabled again.  Eligibility uses the authenticated submission's
creation time on Meta (``provider_created_at >= crm_create_from``), not its local
import time.  Submissions without a provider creation time are excluded under
this policy.

To include earlier history, explicitly choose an earlier cutoff or select
``All submissions``.  The latter includes authenticated submissions regardless
of their provider creation time.  Configuration changes affect only submissions
that have not yet produced a CRM record; completed projections keep their
existing record, assignment and attribution link.

Reconciliation and upgrades
--------------------------

``Reconcile CRM records`` scans submissions in bounded, ID-ordered pages and
respects the route's current history policy.  Each job atomically chains the next
cursor, so reconciliation is resumable.  An older submission becomes eligible
only after the cutoff is moved back or all history is explicitly selected.
Reconciliation and retries never duplicate a completed projection.

Installation enqueues one bootstrap job per company using the same bounded
processing.  Upgrades preserve the previous all-submissions scope of existing
enabled routes and leave explicit lead/opportunity choices unchanged.  An upgrade
does not start historical reconciliation automatically.  New routes use the
date-based default.

CRM owns the projected lead lifecycle. Deleting that lead clears only the live
navigation reference; the Meta projection and assertion retain the original
model, record ID, company and bounded display reference for audit. No deleted
lead is recreated by retry or exposed by an action.

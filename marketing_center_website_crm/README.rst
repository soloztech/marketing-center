Marketing Center - Website CRM Bridge
=====================================

Optional bridge between successful native Odoo Website ``crm.lead`` forms and
the provider-neutral Marketing Center ledger. The bridge verifies the existing
short-lived Website receipt after the native form has returned its record ID.
It then stores an immutable processing intent before creating or
replaying the canonical form touchpoint and appending one CRM assertion.

The bridge depends explicitly on ``website_crm`` and composes its final public
controller cooperatively. The Marketing Center route wrapper remains outside the
native controller in the MRO, while Odoo's native phone, geo, visitor-to-lead and
record-insertion hooks remain in the same ``super()`` chain. Marketing correlation
therefore observes a lead created by the native flow; it never substitutes that
flow.

One immutable correlation owns each Website ingress event. Replaying the same
event for the same lead returns the existing correlation; reusing it for another
lead is rejected and never creates a second attribution assertion. Form values
are never copied to the marketing ledger or correlation. A bounded CRM display
reference accompanies the original model, record ID and company so the evidence
remains interpretable after the live lead is deleted; no email, phone or form
answer is duplicated.

Unexpected projection failures move the durable intent through a bounded retry
schedule serviced by ``queue_job`` and a recovery cron. Administrators can
inspect terminal intents and explicitly retry them without changing identity.
Workers may mutate an intent only when the UUID supplied by ``queue_job`` is the
same UUID persisted on that intent; recovery locates active work by the canonical
identity key and repairs a stale pointer without scheduling a duplicate job.
The receipt is validated once and is never persisted or revalidated by a later
job, so recovery remains possible after its TTL. Contract and access failures
are terminal; retryable PostgreSQL errors are re-raised so Odoo rolls back and
retries the complete transaction. A failure before the durable intent exists is
isolated to the bridge savepoint and never rolls back the native lead; only the
exception class is logged. Historical
forms cannot be backfilled safely because older native records contain no signed
event-to-record binding; no heuristic backfill is provided.

Deleting the native CRM lead sets the optional live references on intents,
correlations and assertions to null. Completed evidence remains append-only;
pending work becomes terminal instead of recreating or guessing a target.
Session revocation continues to use the intent's immutable lead identity after a
native CRM merge, so the authority can still invalidate survivor projections.

Session identifier retention
----------------------------

Every new form intent snapshots the explicitly configured endpoint retention
policy and a deadline from the original form time. This applies even when
processing fails and no ingress event has been created. An hourly cleanup erases
at most 100 due intents per run; processing, retries and session reconciliation
also enforce the deadline before using the session. Expiring the corresponding
ingress event erases the intent's session copy in the same transaction.
Session matching also excludes landing events without an explicit future
deadline and erased identifiers, even before scheduled cleanup runs.

Erasure clears both the session UUID and its comparison hash, disables queued
processing/reconciliation, and leaves a first-wins event tombstone. A completed
intent retains its historical correlation; a pending intent becomes terminal.
Neither replay nor manual retry restores identifiers. The native CRM lead and
previous attribution assertions are preserved; this is identifier minimization,
not deletion of the native CRM record or anonymization of the historical graph.

Legacy intents without a deadline are visible in the intent list with an empty
``Retain Until`` and a read-only ``Proposed Retain Until``. They do not acquire an
inferred duration and cannot perform new correlation until an operator explicitly
applies a documented endpoint policy using ``Apply policy to legacy intent``.
Capture can remain disabled. The action uses the original form date, records the
actor and policy version, accepts at most 100 selected intents through the model
service, and never extends an existing deadline. Review the proposed deadline
before applying it; already expired sessions are eligible for cleanup. Existing
completed evidence remains unchanged while awaiting that decision.

Marketing Center - Contact Center Bridge
=========================================

Optional, one-way projection from Contact Center acquisition evidence to the
Marketing Center touchpoint ledger.  Neither core depends on this addon.
Only links emitted by the current mapper contract participate in downstream CRM
convergence; mapper changes are handled by explicit versioned migrations.
Live attribution and lifecycle wake-ups are scoped to the source PostgreSQL
transaction, so repeated observations in one commit coalesce while a later commit
cannot be hidden by an older job already running on a prior snapshot. Backfills use
separate stable identities and every projection remains idempotent at the ledger.

The same optional bridge emits provider-neutral lifecycle facts without making
either core depend on the other.  The conversation-level projection keeps
``conversation_started`` and the first conversation-level
``first_human_response``.  The operational response ledger additionally starts an
``interaction_started`` episode when an inbound request arrives while no response
is pending, and closes that episode on the first confirmed Odoo-agent or external
device response.

Response ordering follows an immutable first-observation signal ledger, not mutable
provider timestamps.  Late receipts therefore cannot rewrite prior episodes.  A
per-conversation cursor makes normal processing incremental, non-blocking advisory
fences require a fresh PostgreSQL snapshot on contention, and install/upgrade hooks
queue a bounded backfill for pre-existing conversations. Live message signals use
message-specific queue identities while the channel fence serializes projection;
this preserves a durable wake-up for a signal committed while an older job is
running on its prior PostgreSQL snapshot.

The response backfill is a durable two-phase seek process.  It first materializes
bounded pages up to a stable conversation cutoff and only then consumes those pages
chronologically.  Messages arriving while history is materialized remain behind that
barrier and are consumed afterward.  Each transaction advances a persisted frontier
and queues an idempotent continuation, so one long conversation never becomes one
unbounded job or reorders a live signal ahead of older evidence.

Upgrades from the former append-id cursor retain only its proven consumed-id floor.
They materialize the bounded source snapshot first and chronologically drain every
queued or newly materialized signal above that floor before adopting the new seek
cursor.  An upgrade therefore neither assumes that the old queue was empty nor
replays episodes already proven consumed.

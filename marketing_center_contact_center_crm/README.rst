=============================================
Marketing Center - Contact Center CRM Bridge
=============================================

Optional glue between the Contact Center conversation CRM integration and Marketing Center.

The addon projects every Marketing attribution touchpoint observed in a Contact
Center conversation to every CRM lead explicitly linked through that
conversation.  It preserves both many-to-many directions: one
conversation can carry several leads and one lead can participate in several conversations.

Both source ledgers remain immutable.  Each Contact Center conversation link owns a
separate ``contact_center.conversation`` assertion under its conversation ID.
Missing assertions are created by ``marketing.crm.service`` under advisory locks
and database uniqueness, so live hooks, explicit replays and installation
backfill are idempotent.  Unlinking a conversation-to-lead relation atomically appends
revocations for that conversation authority before removing the source relation; links
asserted by manual or future integrations remain effective.

Live convergence is asynchronous and bounded.  Creation hooks enqueue one
durable cursor job for the newly-created side of the graph; each job processes
at most 100 records from the opposite side and chains a monotonic page when
needed.  The installation backfill also scans 50 conversation links per job and only
enqueues bounded child jobs.  This keeps request latency independent from the
number of historic touchpoints and CRM associations while preserving eventual,
idempotent convergence.

The addon is also the final ``crm.lead`` lock-order orchestrator.  Stage changes
and native merges acquire the Contact Center conversation graph (and optional Kanban catalog/binding graph) before
Marketing Center locks CRM lead rows, regardless of the dependency models'
runtime MRO.

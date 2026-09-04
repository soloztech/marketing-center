=============================================
Marketing Center - Contact Center CRM Bridge
=============================================

Optional glue between the Contact Center CRM case bridge and Marketing Center.

The addon projects every Marketing attribution touchpoint observed in a Contact
Center conversation to every CRM lead explicitly linked through that
conversation's cases.  It preserves both many-to-many directions: one
conversation can carry several cases/leads and one lead can participate in cases
from several conversations.

Both source ledgers remain immutable.  Each Contact Center case link owns a
separate ``contact_center.case`` assertion under its stable ``case_ref``.
Missing assertions are created by ``marketing.crm.service`` under advisory locks
and database uniqueness, so live hooks, explicit replays and installation
backfill are idempotent.  Unlinking a case-to-lead relation atomically appends
revocations for that case authority before removing the source relation; links
asserted by manual or future integrations remain effective.

Live convergence is asynchronous and bounded.  Creation hooks enqueue one
durable cursor job for the newly-created side of the graph; each job processes
at most 100 records from the opposite side and chains a monotonic page when
needed.  The installation backfill also scans 50 case links per job and only
enqueues bounded child jobs.  This keeps request latency independent from the
number of historic touchpoints and CRM cases while preserving eventual,
idempotent convergence.

The addon is also the final ``crm.lead`` lock-order orchestrator.  Stage changes
and native merges acquire the Contact Center catalog/binding/core graph before
Marketing Center locks CRM lead rows, regardless of the dependency models'
runtime MRO.

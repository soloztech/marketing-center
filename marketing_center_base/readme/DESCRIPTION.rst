Provider-neutral marketing core for Odoo 16. It contains the canonical immutable
marketing touchpoint ledger, a revisioned external catalog, fenced synchronization
runs and cursors, and daily account/campaign performance facts. Performance keeps
exact BIGINT counters, cost in micros, explicit missing-versus-zero semantics and
immutable A-B-A revision history. Read access is restricted by active company and,
for operational catalog and performance records, by the source roster.

It deliberately has no dependency on Contact Center, CRM, Website, Meta or Google
addons. Provider and bridge addons normalize their responses into the local DTOs and
internal ingestion services exposed by this core. Contact Center keeps its own
operational attribution evidence and connects to this canonical marketing ledger
only through the optional bridge addon.

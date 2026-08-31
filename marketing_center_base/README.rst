Marketing Center Base
=====================

Provider-neutral marketing core for Odoo 16. It contains immutable marketing
touchpoints, a revisioned external catalog, fenced synchronization runs/cursors and
daily account/campaign performance facts. Performance keeps exact BIGINT counters,
cost in micros, explicit missing-versus-zero semantics and immutable A-B-A revision
history. Read access is restricted by company and source roster.

It deliberately has no dependency on Contact Center, CRM, Website, Meta or Google
addons. Provider addons normalize their responses into the local DTOs exposed by
this core.

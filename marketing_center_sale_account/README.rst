Marketing Center - Sales Accounting Bridge
==========================================

This technical bridge owns the causal projection between Accounting and Sales.
It deliberately stays outside both domain addons so ``marketing_center_account``
can emit accounting facts without depending on Sales, while
``marketing_center_sale`` can model the commercial lifecycle independently.

Causal contract
---------------

* The only accepted invoice-to-order evidence is
  ``account.move.invoice_line_ids.sale_line_ids.order_id``.
* ``invoice_origin``, document names, references, partner matching and free text
  are never parsed or used as causal evidence.
* A business-event occurrence receives a snapshot of the typed sales orders
  observed when its accounting event-to-move role is projected.
* A projection receipt is immutable, including when zero orders were observed.
  Replaying the same accounting link therefore cannot attach a later order to
  an older event.
* Reposting a draft invoice creates a new accounting occurrence. That new
  occurrence may snapshot a different typed order without rewriting the older
  occurrence.
* One invoice occurrence may point to multiple sales orders. The bridge creates
  one causal edge per typed order, but never splits, copies or allocates the
  business-event amount across those edges. Reports traversing the graph must
  deduplicate by ``event.id`` and aggregate ``amount_signed_micros`` once per
  event, otherwise a multi-order invoice would be counted more than once.
* ``marketing.sale.service._link_event_order`` owns the downstream propagation
  from business event to sales order and to the CRM lead links already managed
  by ``marketing_center_sale``.

The append-only ``marketing.account.move.sale.link`` records that a journal
entry has had typed commercial evidence for a sales order. Occurrence-specific
causality remains in ``marketing.business.event.sale.link``; those two grains
must not be interpreted as interchangeable.

Late installation and explicit backfill
---------------------------------------

Existing accounting event-to-move links are not projected automatically during
module installation. This avoids an unbounded install transaction. A manager
may run bounded pages explicitly::

    env["marketing.account.service"]._backfill_sale_account_projections(
        company,
        after_id=0,
        limit=500,
    )

The result contains ``processed``, ``projected``,
``skipped_missing_source``, ``last_id`` and ``has_more``.
Continue from ``last_id`` while ``has_more`` is true. The first projection
snapshots the typed invoice lines that exist at that moment and writes a receipt
even when the snapshot is empty. Subsequent executions are idempotent.

An Accounting or Sales document may later be deleted by its native lifecycle.
The typed append-only edge then keeps bounded identities for both original
records while its live navigation fields become empty. Backfill advances past a
missing journal entry and increments ``skipped_missing_source``; it never
fabricates typed causality after the source graph has disappeared.

The first projection also writes an internal claim while the accounting link
and journal entry are locked. This gives concurrent live replay/backfill workers
a PostgreSQL serialization conflict, handled by the normal Odoo transaction
retry, rather than allowing them to race the immutable receipt or typed-link
unique constraints.

Posting counterevents preserve causality
----------------------------------------

An invoice/credit posting counterevent copies the Sales and CRM edges observed
on its original posting when it is reversed. It never reads the draft's current
``sale_line_ids`` or expands the current order-to-CRM graph. This applies even if
typed invoice lines were changed while posted or in the same write that returns
the document to draft. A later reposting snapshots its own current typed graph.

Later legitimate CRM evidence enriches both the source posting and its posting
counterevent under the Sales convergence contract. These appended edges preserve
zero net revenue for a fully reversed posting when a lead is associated later;
they do not reread the draft invoice's current typed lines.
Missing native orders/leads cannot create live navigation; their historical edges
remain accessible through the counterevent's immutable ``reverses_event_id``.

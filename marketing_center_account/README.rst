Marketing Center - Accounting
=============================

This optional bridge projects first-party Accounting facts into the immutable
``marketing.business.event`` ledger. It records:

* posted customer invoices in their transaction currency, using the untaxed
  amount as an explicitly named basis;
* posted customer credit notes as negative amounts, linked to the exact posted
  invoice event when Odoo supplies ``reversed_entry_id``;
* each inbound customer-payment allocation represented by one
  ``account.partial.reconcile``; and
* the exact negative reversal when that partial reconciliation is removed.

Payment allocation is deliberately conservative. It is emitted only when one
side is a posted customer invoice receivable and the other side belongs to a
posted inbound customer ``account.payment`` for the same commercial partner.
The amount is the partial reconcile's authoritative company-currency amount.
Credit-note offsets, vendor flows, outbound refunds and statement-only matches
are not relabelled as cash receipts, and no foreign-exchange allocation is
invented.

Invoice value and payment allocation are separate grains and amount bases.
Dashboards must never add them into one revenue total: one answers “what was
invoiced”, while the other answers “what exact receivable allocation occurred”.
The typed journal-entry and payment relations are graph edges only; traversing
multiple edges must not duplicate an event's amount.

This addon deliberately depends only on Accounting and the provider-neutral
Marketing Center ledger. Optional typed convergence with sales orders and CRM
leads lives in ``marketing_center_sale_account``. That glue uses Odoo's
``sale_line_ids`` relation; names and free-form ``invoice_origin`` values are
never accepted as causal evidence.

Historical reconciliation is explicit and paginated. Installing the addon does
not run an automatic backfill. Allocation backfill covers only surviving
``account.partial.reconcile`` rows; a partial that was deleted before this addon
observed it has no authoritative amount left and is therefore not reconstructed
or labelled as a reversal.

The first allocation projection claims its partial-reconcile row inside the
same transaction. This creates a real MVCC serialization point for concurrent
live processing and backfill; a transaction that loses that race must be retried
with the standard Odoo transaction retry so it can observe the committed event.

The evidence ledger also does not own Accounting document retention. A normal
invoice, journal-entry or payment deletion nulls its live navigation edge while
the append-only link keeps a bounded ``model``/original ``res_id``/reference
snapshot. Event counts and navigation actions intentionally include only live
records; historical evidence remains queryable without creating a dangling
Odoo action or preventing Accounting cleanup.

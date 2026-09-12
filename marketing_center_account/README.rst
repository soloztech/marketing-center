Marketing Center - Accounting
=============================

This optional bridge projects first-party Accounting facts into the immutable
``marketing.business.event`` ledger. It records:

* posted customer invoices in their transaction currency, using the untaxed
  amount as an explicitly named basis;
* posted customer credit notes as negative amounts, with the native typed
  ``reversed_entry_id`` relationship retained independently of monetary eligibility;
* exact counterevents on posted-to-draft/cancel transitions for invoices and
  credit notes, followed by a new occurrence on reposting;
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

Posting lifecycle and economic projections
------------------------------------------

``invoice_posting_reversed`` negates exactly one ``invoice_posted`` occurrence;
``credit_note_posting_reversed`` negates exactly one ``credit_note_posted``
occurrence, including a credit that itself references an invoice. Both retain
original currency and amount, and a database index permits only one counterevent
per posting. Sales cancellations and payment reversals retain their prior rules.
No old occurrence or edge is modified when a document is reposted. A credit
against an earlier invoice posting continues to refer to that historical event.

``marketing.business.event.service._active_posting_events(events)`` returns
currently effective postings. Sum those amounts by company, currency and amount
basis for a current projection. For a period's flow, sum all four posting and
counterevent types by their occurrence timestamp within the period; an invoice
posted in August and returned to draft in September contributes a positive August
flow and a negative September flow. Do not filter first to current postings when
computing those historical flows. Document counts remain occurrence counters and
are not monetary balances. No cross-currency conversion is performed.

An optional ``reverses_event_id`` on a credit represents a same-currency credit
within the available invoice amount. Invalidated credits release that budget;
the Accounting adapter also includes still-effective credits tied to older
postings of the same native invoice. Odoo may legitimately post larger credits,
credits against draft documents or credits in another currency. Those remain
independent negative facts with a ``reversed_invoice`` typed relation and an
explicit ``account.credit_reversal_disposition`` reason; this analytic ceiling
does not reject the native posting. Unexpected ledger errors are not swallowed.

Live posting and unposting are transactional, with ordered locks on each document
and its reversal source. A physical no-op update of those native rows establishes
an MVCC serialization point for concurrent credits and backfill. The normal Odoo
transaction retry obtains a fresh snapshot. Counterevent identity is derived from
the immutable posting reference, so repeats cannot double-cancel it.

New snapshots declare ``account.occurred_at_basis`` as ``observed_transition``
for live changes or ``document_date`` for imported evidence. Date-based backfill
remains an approximation of the posting time. Upgrading does not rewrite or infer
missing historical reversals: old incomplete histories require an explicitly
reviewed reconciliation. Installing this version adds event types and the partial
uniqueness index; there is no automatic financial data migration.

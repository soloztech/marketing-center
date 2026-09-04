Marketing Center Sales
======================

Links sales orders to CRM leads and immutable Marketing Center business events.
It projects the first quotation-sent transition, every real order confirmation,
and the corresponding exact cancellation reversal into the provider-neutral
business-event ledger.

``order_confirmed`` freezes the currency-rounded untaxed commercial value as the
canonical amount (scaled exactly to micros by the base ledger). Gross and tax
amounts remain named snapshot dimensions. ``order_cancelled`` always negates that
frozen confirmation amount, including for zero-value orders.

Historical reconciliation is explicit. It uses stable chatter tracking rows (or
stable record timestamps when no tracking row exists), never a mutable wall-clock
identifier. It does not invent a historical ``proposal_sent`` after the order has
already advanced without evidence; imported revenue uses the current currency-rounded
order projection and is marked with imported evidence.

Evidence links never own the native sales order or CRM lead lifecycle. Deleting
an order, deleting a lead, or merging leads nulls only the live navigation edge;
the append-only link retains a bounded ``model``/original ``res_id``/reference
snapshot. A native CRM merge does not rewrite a historical order-to-lead edge to
the survivor, because that would invent a commercial relationship. A later
explicit association appends a new canonical edge instead.

=================
Meta Webhook Base
=================

Shared, provider-neutral Meta webhook ingress for Odoo 16. The addon owns one
callback per shared ``meta.api.app``, validates the exact request-body HMAC,
stores only bounded sanitized evidence, and dispatches immutable items to
consumer addons through an abstract model.

One request admits at most 1,000 delivery items in total, independent of how
they are distributed across entries. Queue creation is serialized on the
persisted delivery/dispatch/endpoint rows and workers revalidate the persisted
state after taking their lock, so stale ORM caches cannot duplicate work or
revive terminal evidence.

It deliberately contains no Contact Center, Marketing Center, CRM, Messenger,
Instagram or Lead Ads business projection. Those addons register subscriptions
and extend ``meta.webhook.dispatcher``.

Credentials
===========

App secrets, verify tokens and Page tokens are external references resolved by
``meta_api_base``. Secret values are never model fields. Configure HTTPS
``web.base.url`` before reconciling subscriptions.

Consumer Graph runtime
======================

``meta.webhook.page._resolve_graph_runtime()`` returns one fenced, in-memory
App runtime, the resolved Page token and the Page revision. Page, endpoint and
App rows are share-locked while active state and expected revisions are checked.
Secret values are never written to an Odoo model.
The method requires the identity-only ``META_WEBHOOK_RUNTIME_TOKEN`` context
capability exported by the Python package. RPC-serializable context values are
rejected, keeping resolved credentials inside trusted server-side addons.

Subscription ownership
======================

The endpoint reconciler publishes the union of every active consumer field:

* App callbacks use ``/{APP_ID}/subscriptions`` grouped by Meta object.
* Page-linked installation uses ``/{PAGE_ID}/subscribed_apps`` and the union
  of active fields for the Graph ``page`` object. Instagram fields stay at the
  App callback layer and are not sent to the Page edge.

Both Graph readbacks follow only bounded opaque cursors on the same fixed edge;
provider ``next`` URLs are never fetched.

A material ``meta.api.app`` change invalidates every dependent endpoint observation
under the same Endpoint-before-App lock order used by reconciliation. Pausing the App
projects an explicit technical error; resuming or rotating its configuration returns
the endpoint to ``unknown`` until a fresh readback. App revision is part of the queue
identity, so a job created for an older credential fence cannot shadow the new job.

Empty unions are never removed automatically. They are reported as drift for a
system administrator, avoiding an accidental destructive unsubscribe.

The endpoint and Page reconciliation actions return the queued job UUID as a
plain string, so the same actions are safe to invoke from the Odoo UI or RPC.

``META_WEBHOOK_FRESHNESS`` is the shared 30-minute observation contract for
consumer addons. Active endpoints are checked every 10 minutes; ``unknown`` and
``error`` observations are eligible immediately, while other observations are
refreshed after 10 minutes, comfortably inside that public window. Each run is
bounded to 20 endpoints and delegates to the same identity-key deduplicated
queue service. ``meta_webhook_base.subscription_refresh_seconds`` (5 to 15
minutes) and ``meta_webhook_base.reconcile_batch_limit`` (1 to 100) tune only
the scheduling bounds; the public freshness contract remains shared and fixed.

Consumer extension
==================

Consumer addons inherit ``meta.webhook.dispatcher``. They may sanitize
``entry[].messaging`` into immutable item specifications in
``_consumer_item_specs`` and handle only their own ``consumer_key`` in
``_dispatch_consumer``. Provider-private or expiring artifacts must remain in
the consumer addon; the shared item stores only a safe reference.

Operational notes
=================

Delivery, item and dispatch ledgers cannot be deleted. Queue retries are
bounded, consumer/provider exception text is replaced by stable shared errors,
unexpected failures close terminally, and a cron recovers orphaned
``pending``/``processing`` jobs after worker interruption.
Routing keys are namespaced by Meta object type, so equal numeric identifiers in
different Graph namespaces cannot cross-route.

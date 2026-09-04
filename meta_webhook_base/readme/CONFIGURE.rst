Create a shared ``meta.api.app``, then a webhook endpoint and its external
verify-token reference. Register each Facebook Page token and any Page-linked
Instagram Professional Account routing asset. Consumer addons register their
desired object/field subscriptions. Reconciliation installs each object/field
union on the App callback. On ``/{PAGE_ID}/subscribed_apps`` it installs only
the ``page`` object field union; ``instagram`` fields are never mixed into that
Page edge. It never sends an empty ``subscribed_fields`` mutation. Run
**Reconcile Meta subscriptions** after setting an HTTPS ``web.base.url``.

The endpoint and Page reconciliation actions return the queued job UUID as a
plain string. RPC clients can use that UUID to follow the job without receiving
an Odoo record proxy that XML-RPC cannot marshal.

The periodic reconciler runs every 10 minutes, checks at most 20 active
endpoints and refreshes observations after 10 minutes, inside the public
30-minute ``META_WEBHOOK_FRESHNESS`` contract. Optional system parameters
``meta_webhook_base.subscription_refresh_seconds`` (300..900) and
``meta_webhook_base.reconcile_batch_limit`` (1..100) tune scheduling bounds.

Reconciliation is endpoint-scoped. Concurrent cron/UI requests reuse the same
active queue job after locking and revalidating the persisted endpoint/App
configuration. Provider pagination follows only bounded opaque cursors on the
same fixed Graph edge; provider-supplied ``next`` URLs are never fetched.

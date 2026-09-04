Consumer addons inherit ``meta.webhook.dispatcher``. They call ``super()`` and append
sanitized ``entry[].messaging`` item specifications in ``_consumer_item_specs``, then
handle only their own ``consumer_key`` in ``_dispatch_consumer``. Provider-private
or expiring artifacts stay in the consumer addon; the shared item stores only a safe
reference.

Delivery, item and dispatch ledgers cannot be deleted. Queue retries are bounded,
consumer/provider exception text is replaced by stable shared errors, unexpected
failures close terminally and a cron recovers orphaned ``pending`` or ``processing``
jobs after worker interruption. Routing keys include the Meta object namespace, so
equal numeric identifiers in different Graph namespaces cannot cross-route.

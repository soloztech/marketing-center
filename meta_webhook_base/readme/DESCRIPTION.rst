Shared, provider-neutral Meta webhook ingress for Odoo 16. The addon owns one
callback per shared ``meta.api.app``, validates the exact request-body HMAC, stores
only bounded sanitized evidence and dispatches immutable items to consumer addons
through an abstract model.

One request admits at most 1,000 delivery items in total, independent of how they are
distributed across entries. Queue creation is serialized on persisted delivery,
dispatch and endpoint rows. Workers lock and revalidate persisted state, preventing
stale ORM caches from duplicating work or reviving terminal evidence.

It deliberately contains no Contact Center, Marketing Center, CRM, Messenger,
Instagram or Lead Ads business projection. Consumer addons register subscriptions,
keep any provider-private or expiring artifacts in their own domain and extend
``meta.webhook.dispatcher`` only for their own routing key.

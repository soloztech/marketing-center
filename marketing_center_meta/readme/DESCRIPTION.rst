Read-only Meta Marketing API connector for ``marketing_center_base``. It uses the
shared ``meta_api_base`` transport, validates reader tokens asynchronously and
discovers Meta ad accounts into provider-neutral source and connection models. Each
discovered account can enqueue a catalog sweep into the canonical
``marketing.center.sync.run``, cursor and external-entity ledgers.

The catalog uses one ordered, cursor-fenced run per account: campaign, ad set
(``group`` / ``meta_adset``), ad and reusable creative. Each job fetches one bounded
page with fixed ``GET`` fields, and the opaque ``after`` cursor is wrapped in a
versioned local stage envelope. The provider ``paging.next`` URL is never followed
or persisted. Missing catalog objects are not tombstoned by this first sweep because
a page scan alone is not authoritative deletion evidence.

The Insights reader projects the last seven closed days at account and campaign
grain. Its initial contract requests only impressions, clicks and spend, converts
spend exactly to micros, respects the ad-account timezone and preserves missing
metrics as missing. Actions, conversions, attribution settings and breakdowns are
intentionally outside this contract.

Lead Ads routes bind one App, Page and Instant Form. A signed webhook callback stores
only a durable, sanitized routing hint and queues an authenticated Graph v26 GET.
Private ``field_data`` answers are restricted to system administrators; the
provider-neutral attribution ledger receives only hashed or masked identifiers and
safe asset references. A bounded, cursor-based pull reconciler repairs missed
callbacks through the same idempotent submission and touchpoint projection. It does
not create or update CRM leads.

UI actions enqueue OCA jobs; no Meta request runs in the browser request transaction.
Campaign mutation, automatic CRM lead creation and CAPI remain disabled.

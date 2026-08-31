Marketing Center - Meta
=======================

Read-only Meta Marketing API connector for ``marketing_center_base``. It uses the
shared ``meta_api_base`` transport, validates a reader token asynchronously and
discovers Meta ad accounts into the provider-neutral source and connection models.
Each discovered account can enqueue a read-only catalog sweep into the canonical
``marketing.center.sync.run``, cursor and external-entity ledgers.

No App secret or access token is stored in PostgreSQL. A profile stores two opaque
references using one of these backends:

* ``environment``: environment variable names such as
  ``ODOO_META_MARKETING_APP_SECRET`` and ``ODOO_META_MARKETING_ACCESS_TOKEN``;
* ``file``: filenames below the directory named by ``ODOO_META_API_SECRET_DIR``.
  Files must be regular, non-symlink files without group/world permissions.

The catalog uses one ordered, cursor-fenced run per account: campaign, ad set
(``group`` / ``meta_adset``), ad and reusable creative. Each job fetches one bounded
page with fixed ``GET`` fields, and the opaque ``after`` cursor is wrapped in a
versioned local stage envelope. The provider ``paging.next`` URL is never followed or
persisted. Missing objects are not tombstoned by this first sweep because a page scan
alone is not yet authoritative deletion evidence.

The Insights reader projects the last seven closed days at account and campaign
grain. Its v1 contract requests only impressions, clicks and spend, converts spend
exactly to micros, respects the ad-account timezone and preserves missing metrics as
missing. Actions, conversions, attribution settings and breakdowns are intentionally
outside this first contract.

UI buttons enqueue OCA jobs; no Meta request runs in the browser request transaction.
Campaign mutation, Lead Ads ingestion and CAPI remain disabled.

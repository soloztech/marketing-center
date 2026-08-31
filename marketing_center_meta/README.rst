Marketing Center - Meta
=======================

Read-only Meta Marketing API connector for ``marketing_center_base``. It uses the
shared ``meta_api_base`` transport, validates a reader token asynchronously and
discovers Meta ad accounts into the provider-neutral source and connection models.

No App secret or access token is stored in PostgreSQL. A profile stores two opaque
references using one of these backends:

* ``environment``: environment variable names such as
  ``ODOO_META_MARKETING_APP_SECRET`` and ``ODOO_META_MARKETING_ACCESS_TOKEN``;
* ``file``: filenames below the directory named by ``ODOO_META_API_SECRET_DIR``.
  Files must be regular, non-symlink files without group/world permissions.

The first release exposes only ``GET`` discovery. UI buttons enqueue OCA jobs; no
Meta request runs in the browser request transaction. Campaign mutation, Insights,
Lead Ads ingestion and CAPI remain disabled.

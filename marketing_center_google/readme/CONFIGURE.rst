Create and validate a company-scoped ``google.api.identity`` in the shared
integration foundation. Then create a Google Ads reader profile under Marketing
Center configuration and run **Validate** followed by **Discover customers**.

Discovery is read-only, starts at the configured login customer when present,
and traverses only the bounded ``customer_client`` hierarchy. Leaf customers are
projected into canonical Marketing Center sources and reader connections.

Catalog and closed daily performance jobs use fixed Google Ads v25 GAQL
allowlists. Every new sweep starts at its first page; provider page tokens are
valid only inside the run that received them.

No credential value, access token, service-account document, developer token,
or raw provider response is stored by this addon.

Exact click lookup is separately opt-in per Google Ads source through
**Resolve captured Google clicks**. It uses an eligible protected GCLID, one
account-local day and one unambiguous enabled account. Native classification
can remain in Simulation while lookup is enabled. Missing catalog entities
resolve when the existing catalog synchronization receives them.

See the `native campaign guide <../docs/native-campaign-attribution.md>`_ for
privacy, retry, retention, CRM mapping and rollout details.

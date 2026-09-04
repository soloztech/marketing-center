Read-only Google Ads v25 consumer for Marketing Center. The addon discovers
accessible customers, projects them into provider-neutral sources and connections,
and synchronizes a versioned catalog plus closed daily performance facts through
fixed GAQL contracts.

Change History is collected in finite, closed local-date windows: three days by the
daily cron and seven days from the manual action, with a hard maximum of 30 days.
Google requires a query limit no greater than 10,000 rows, so a window reaching that
boundary fails safely instead of being reported as complete. The immutable ledger
stores only allowlisted event metadata. It never requests or persists
``old_resource`` or ``new_resource``; the Google actor email is converted
immediately to a SHA-256 digest.

Delivery diagnostics take a bounded daily snapshot of campaigns, ad groups and ads.
The ledger keeps configured/primary status, primary-status reasons and a minimal
policy summary. Policy evidence and raw provider payloads are not stored. Replaying
the same synchronization run is idempotent, while a new run preserves a new
immutable observation even when an asset returns to a previous state.

Catalog, performance and observability flows reuse the core synchronization
run/cursor fencing, queue channel, quota cooldown and profile/identity revision
guards. Managers can read only observations for sources in their team roster;
Marketing Administrators can read every observation in their active companies and
are the only users who can launch manual synchronization.

Authentication and developer-token secrets remain exclusively in the external
secret backend resolved by ``google_api_base``. No credential, refresh token,
developer token or raw provider response is copied to a Marketing Center ledger.
This addon exposes no Google Ads mutation method through its adapter, jobs or user
interface.

The implemented provider contracts follow:

* https://developers.google.com/google-ads/api/docs/change-event
* https://developers.google.com/google-ads/api/fields/v25/campaign
* https://developers.google.com/google-ads/api/fields/v25/ad_group
* https://developers.google.com/google-ads/api/fields/v25/ad_group_ad

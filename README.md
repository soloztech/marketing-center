# Integration Core

Technical integration primitives shared by independent Odoo domains. This repository
must not contain Contact Center, Marketing Center, CRM, Website or other business
rules.

## Addons

- `meta_api_base`: bounded Meta Graph transport, neutral error contract, exact-byte
  webhook HMAC verification, safe external-secret resolution and the shared
  `meta.api.app` configuration/fencing boundary. Version `16.0.1.1.1` depends only on
  Odoo `base`.
- `meta_webhook_base`: shared Meta callback, bounded sanitized delivery ledger,
  object-namespaced routing, consumer dispatch and subscription reconciliation.
  Version `16.0.1.4.2` depends on `meta_api_base` and OCA `queue_job`; it has no
  Contact Center, Marketing, CRM or Website projection.
- `google_api_base`: read-only Google Ads REST v25 transport, OAuth refresh,
  revision-fenced external credentials, generic DTOs and bounded discovery/Search
  pagination. Version `16.0.1.1.2` depends only on Odoo `base` plus its declared
  Python transports.

`contact_center_meta` and `marketing_center_meta` may both depend on
the shared Meta foundations; they never depend on each other. The App, webhook
admission ledger and delivery queue are shared technical contracts. Authorizations,
provider-private artifacts and business DTO projections remain consumer-owned.

## Security contract

- only opaque environment/file references are persisted; secret values are resolved
  into immutable runtime objects and remain in process memory;
- access tokens and App Secrets never enter exception messages;
- redirects are disabled and response bodies are streamed under a hard byte limit;
- `POST` and `DELETE` are always treated as mutations with uncertain-outcome handling;
- webhook signatures are checked over the exact raw bytes and reject bodies above the
  shared limit. Public controllers must also cap the request while reading it, before
  the full body is materialized.

## Current greenfield baseline

The final no-migrations baseline was applied and validated on SERVIDOR05 with the
versions above. It passed 45/45 `meta_api_base`, 73/73 `meta_webhook_base` and 42/42
`google_api_base` tests (160/160 total), offline apply/replay, exact addon-path and
module-state isolation, and restored internal/public HTTP 200. The 79-file tree hash
is `cec6ebd0d95a8797430163be30e1c25f834cb4de7532cc2e65eca463d766b3af`.

Pre-production migrations were squashed before this first production-target baseline.
Older development schemas are not supported upgrade origins. After the first go-live,
every persistent schema/data change must again ship with a cumulative versioned
migration.

See the [greenfield closeout](reviews/2026-09-04-greenfield-closeout.md) and its
[independent-review disposition](reviews/2026-09-04-independent-review-disposition.md).
The shared boundary and evidence index is documented in the
[cross-repository baseline](../reviews/2026-09-04-greenfield-baseline-cross-repo.md).
The earlier 23-test foundation remains historical evidence in
[the initial validation](reviews/2026-08-31-meta-api-base-foundation.md).

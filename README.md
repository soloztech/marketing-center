# Integration Core

Technical integration primitives shared by independent Odoo domains. This repository
must not contain Contact Center, Marketing Center, CRM, Website or other business
rules.

## Addons

- `meta_api_base`: bounded Meta Graph transport, neutral error contract and exact-byte
  webhook HMAC verification. Version `16.0.1.0.0` is installed and validated in the
  disposable SERVIDOR05 laboratory. It depends only on Odoo `base`.
- `google_api_base`: planned after the Odoo 16 Python/SDK compatibility spike.

`contact_center_meta` and `marketing_center_meta` may both depend on
`meta_api_base`; they never depend on each other. Consumer-specific apps,
authorizations, webhook routing, ledgers, queues and DTO normalization remain in their
own addons until an additive shared contract is proven.

## Security contract

- credentials are supplied by the caller and are never persisted by this addon;
- access tokens and App Secrets never enter exception messages;
- redirects are disabled and response bodies are streamed under a hard byte limit;
- `POST` and `DELETE` are always treated as mutations with uncertain-outcome handling;
- webhook signatures are checked over the exact raw bytes and reject bodies above the
  shared limit. Public controllers must also cap the request while reading it, before
  the full body is materialized.

The first laboratory release passed 23/23 isolated tests, install plus idempotent
upgrade replay, unique addon-path resolution and module-state isolation. See the
[foundation validation](reviews/2026-08-31-meta-api-base-foundation.md).


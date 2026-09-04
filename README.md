# Marketing Center

[![Pre-commit Status](https://github.com/soloztech/marketing-center/actions/workflows/pre-commit.yml/badge.svg?branch=16.0)](https://github.com/soloztech/marketing-center/actions/workflows/pre-commit.yml?query=branch%3A16.0)
[![Build Status](https://github.com/soloztech/marketing-center/actions/workflows/test.yml/badge.svg?branch=16.0)](https://github.com/soloztech/marketing-center/actions/workflows/test.yml?query=branch%3A16.0)

Provider-neutral marketing operations, attribution and native Odoo business-event
integration for Odoo 16. The repository also hosts the small shared Google and Meta
transport foundations used by Marketing Center and Contact Center.

Physical co-location does not merge responsibilities: every directory remains an
independent Odoo addon with its own manifest, dependencies, security and tests.
Installing `contact_center_meta` requires the Meta foundations from this repository,
but does not require `marketing_center_base` or any functional Marketing Center addon.

## Shared provider foundations

- `meta_api_base` — bounded Meta Graph transport, external-secret resolution,
  application identity and neutral provider errors;
- `meta_webhook_base` — authenticated Meta webhook ingress, sanitized technical
  delivery ledger, deduplication, dispatch and subscription reconciliation;
- `google_api_base` — bounded Google REST/OAuth transport, external credentials and
  provider-neutral request contracts.

These addons contain no campaign, attribution, conversation, lead, Website, Sale or
Accounting business rules.

## Marketing Center addons

- `marketing_center_base` — canonical sources, catalog, metrics, touchpoints,
  attribution and business events;
- `marketing_center_google` and `marketing_center_meta` — provider adapters;
- `marketing_center_web_ingress` and `marketing_center_website` — first-party web
  evidence and Odoo Website adapter;
- `marketing_center_crm`, `marketing_center_sale` and `marketing_center_account` —
  projections over native Odoo domains;
- `marketing_center_dashboard` — management read model;
- bridge addons keep optional domains decoupled;
- `marketing_center_suite` is the complete Soloz installation profile.

See [ARCHITECTURE.md](ARCHITECTURE.md) for boundaries and installation profiles and
[plan.md](plan.md) for the delivery record and remaining production gates.

## Current greenfield baseline

The pre-production development lineage was squashed before the first production
candidate. Older laboratory schemas are not supported upgrade origins. After the first
go-live, every persistent schema or data change must ship with a cumulative versioned
migration.

The three provider foundations previously lived in the local `integration-core`
checkout. Their Git ancestry was merged into this repository before the first GitHub
release. Module technical names, models, tables and dependency contracts did not
change; therefore this source consolidation requires no Odoo database migration.

Production has not been changed by this repository release.

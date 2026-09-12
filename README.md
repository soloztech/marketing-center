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

## Community content catalog

[Marketing Center Catalog](catalog-plan.md) is an independently installable
content application for company information, product families and variants, documents,
media and FAQs. It uses direct editing without an editorial approval workflow.

- `marketing_center_catalog` — standalone content catalog using native Odoo modules;
- `marketing_center_catalog_contact_center` — optional conversation popup and composer integration;
- `marketing_center_catalog_sale` — optional access from quotations and sales orders.

These addons do not require `marketing_center_base` or the attribution bridges and
are not included in `marketing_center_suite`. The catalog has its own menu and
Reader/Editor groups. Grant access through the user settings after installation.

Install `marketing_center_catalog` for the library alone, add
`marketing_center_catalog_sale` for quotations, or add
`marketing_center_catalog_contact_center` for conversations. The latter needs the
Contact Center repository on the addons path. Source distribution does not make
catalog records or files public. See each addon README for configuration and usage.

Install the repository Python dependencies with its versioned compatibility contract:

```bash
python -m pip install --constraint constraints.txt --requirement requirements.txt
```

The constraint keeps current Google Auth, `cryptography`, `pyOpenSSL` and Odoo's
`urllib3.contrib.pyopenssl` bridge mutually compatible. CI imports that exact TLS stack
before initializing its database, so dependency drift fails before addon tests.

## Current greenfield baseline

The eighteen analytics/provider addons and the three independent content catalog
addons have separate versions in their manifests. The workflow pins the compatible
Contact Center source SHA; `oca_dependencies.txt` is an alternative installation
description, not the CI lock. Use the manifest and workflow from the same reviewed
commit when assembling a release.

The [2026-09-12 implementation record](reviews/2026-09-12-review-fixes-implementation.md)
tracks this candidate and its validation. The [2026-09-05 audit](reviews/2026-09-05-greenfield-audit.md)
and laboratory totals below describe historical source trees.

The attribution calculation engine is an internal foundation. Automatic eligible
candidate production and an attributed-revenue dashboard are not enabled features.
Effective CRM links alone do not authorize credit. See the
[calculation contract](reviews/2026-09-01-attribution-calculation-foundation.md).

Queue capacity, retry exhaustion, public ingress admission and credential rotation
follow the [operations contract](docs/queue-and-credential-operations.md).

The pre-production development lineage was squashed before the first production
candidate. Older laboratory schemas are not supported upgrade origins. After the first
go-live, every persistent schema or data change must ship with a cumulative versioned
migration.

The three provider foundations previously lived in the local `integration-core`
checkout. Their Git ancestry was merged into this repository before the first GitHub
release. Module technical names, models, tables and dependency contracts did not
change; therefore this source consolidation requires no Odoo database migration.

The consolidated laboratory release validated 425 files with tree
`d62f85de79bef0c23b19fd3315bd114d98fe18a8b1bec9e10f2e1d02d8234ceb`.
Seven clean-database suites passed 1,071 tests in total, including all 160 tests of the
three provider foundations. QUnit passed 12/12 tests and 49/49 assertions in minified
and debug-assets modes. All 18 addons were upgraded and replayed idempotently, queues
converged, temporary resources were removed and the former remote `integration-core`
root was removed. See the
[cross-repository baseline](reviews/2026-09-04-greenfield-baseline-cross-repo.md).

The historical validation above does not establish the current production state.
This review-fix candidate is local code and requires its own release evidence.

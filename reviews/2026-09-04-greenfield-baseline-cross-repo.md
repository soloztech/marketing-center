# Greenfield cross-repository baseline

Date: 2026-09-04  
Validated environment: SERVIDOR05, Odoo 16 laboratory  
Production: not accessed or changed

## Repository boundary

The production-target source baseline is published in two private repositories:

| Repository | Physical contents | Functional authority |
| --- | --- | --- |
| `soloztech/contact-center` | five Contact Center addons | conversations, identities, inboxes, routing, cases, SLA and operational evidence |
| `soloztech/marketing-center` | fifteen Marketing Center addons plus three small provider foundations | journeys, attribution, native business projections, and neutral Google/Meta transport and webhook primitives |

`google_api_base`, `meta_api_base` and `meta_webhook_base` are physically co-located
with Marketing Center but remain independent technical addons. They contain no
campaign, conversation, CRM, Website, Sale or Accounting business rule. Their
technical names, models, tables and XML IDs were not changed by the move.

The combined 23-addon graph has 37 internal edges and no addon dependency cycle. The
only cross-repository edges are:

- `contact_center_meta` to `meta_api_base` and `meta_webhook_base`;
- `marketing_center_contact_center` to `contact_center_base`;
- `marketing_center_contact_center_crm` to `contact_center_crm`.

## Validated baselines

| Repository | Tree | Principal gates | Evidence |
| --- | --- | --- | --- |
| Contact Center | 261 files; `83b88b22773067c34e370d3784ec05f1bd0b2b09f08a478230475bbe16bb1f9e` | Base 505/505; WuzAPI 199/199; integrated 895/895; Base QUnit 4/4 and UI QUnit 144/144 in both asset modes | `scans/raw/20260903-odoo16-contact-center-base-crm-greenfield-closeout/release/20260904T124751394613Z/summary.json` |
| Marketing Center and provider foundations | 425 files; `d62f85de79bef0c23b19fd3315bd114d98fe18a8b1bec9e10f2e1d02d8234ceb` | Google API 42/42; Meta API 45/45; Meta Webhook 73/73; base 123/123; integrated 660/660; Website 123/123; suite 5/5; QUnit 12/12 in both asset modes | `scans/raw/20260903-odoo16-marketing-center-remaining-addons-greenfield-closeout/release/20260904T123542490166Z/summary.json` |

Both releases finished as `applied_and_validated`. Offline upgrade/replay gates
returned zero, convergence queues had no active or failed work, every temporary test
database and container was removed, the exact test route was restored with SHA-256
`2fd9e569856478dfa336391bd226f3c8af05454db56033be6352ef80aad56403`,
and the public laboratory endpoint returned HTTP 200.

After consolidation, `meta_api_base` and `meta_webhook_base` each resolve exactly once
inside the Odoo container, under `/mnt/outros/marketing-center`. The former remote
`integration-core` source root is absent.

## Greenfield disposition

Pre-production migrations and migration-only helpers were removed before this first
production-target baseline. An older laboratory snapshot is not an implicitly
supported upgrade origin. After the first production installation, every persistent
schema or data change must again ship as a cumulative, versioned migration from the
last released baseline.

Ledgers, deduplication evidence, guest merge/rebind behavior and optional bridge addons
were retained because they remain runtime contracts, not legacy compatibility.

## Publication policy

- Both GitHub repositories are private and use branch `16.0`.
- The first coordinated candidate uses tag `16.0.20260904.1-rc1` in both repositories;
  each CI workflow checks out the sibling repository at that immutable tag.
- Contact Center is published from a new sanitized root history. The prior local
  development history is retained only in a mode-`0600`, verified Git bundle whose
  SHA-256 is
  `18d3e3b1eb8fb539fb2d36c02aad46bce3d30fc88e8cce2761b6321a371cf464`.
- Marketing Center retains its compact development history, including the ancestry of
  the former provider-foundation repository.
- Cross-repository CI uses a GitHub Actions secret and never stores a credential in
  Git. The bootstrap token must later be replaced by a least-privilege GitHub App with
  `Contents: read` on only these two repositories.
- The OCA Queue `16.0` dependency resolved at validation time to commit
  `4ea642c3930bc2bbaeb760c3410714c2ec9143f1`.

## Remaining production gates

This closes the technical source baseline, not the operational go-live decision. The
remaining gates are:

- a unified retention/LGPD policy with purpose, retention deadline, legal hold and
  paginated purge while preserving the minimum deduplication proof;
- authoritative edge rate limiting for public ingress;
- controlled real-provider failure exercises for timeout, rate limit, cursor loops and
  uncertain delivery;
- dashboard `EXPLAIN (ANALYZE, BUFFERS)` and p95/p99 budgets at representative volume;
- an explicit production cutover and rollback window.

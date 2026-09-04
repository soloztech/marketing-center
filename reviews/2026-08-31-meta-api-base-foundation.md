# Meta API Base — foundation validation

Date: 2026-08-31 Target: disposable neutralized Odoo 16 laboratory on SERVIDOR05 Module:
`meta_api_base` `16.0.1.0.0`

## Outcome

The first provider-neutral Meta runtime is installed and validated independently from
Contact Center and Marketing Center. Its manifest depends only on Odoo `base`; it has no
models, public controller, queue, business DTO or persisted secret.

Delivered contract:

- exact-byte HMAC-SHA256 webhook verification with a shared 2 MiB ceiling;
- explicit Graph version validation;
- fixed-host and path-bounded Graph requests with redirects disabled;
- `appsecret_proof` and application token built only in memory;
- bounded streamed response decoding;
- permanent, transient, paused, rate-limited and uncertain neutral errors;
- `POST` and `DELETE` always receive uncertain-outcome semantics even when a caller
  omits the compatibility flag;
- safe HTTP 408, authorization, permission and rate-limit metadata without provider
  payloads, URLs or credentials in exception text;
- recursive rejection of caller-supplied credential fields.

## Verification

- local unit harness: 23/23;
- Black 22.8, isort 5.12, flake8 5.0.4 + bugbear and `compileall`: clean;
- Odoo isolated database: 23/23, then database and ephemeral container removed;
- main laboratory database: installation succeeded;
- second offline upgrade replay: succeeded;
- installed module set: only `meta_api_base` was added (344 to 345);
- effective runtime: one source path at `/mnt/outros/integration-core/meta_api_base`;
- private and public Odoo probes: HTTP 200;
- Traefik route restored with its original SHA-256;
- production: not accessed or changed;
- database backup: waived for the disposable laboratory as explicitly requested.

Canonical release evidence:

`scans/raw/20260830-odoo16-integration-core-meta-api-base/release/20260831T031224140828Z`

## Recovered pre-install attempts

Two attempts stopped before module installation and recovered source, Odoo processes and
route automatically. The first runtime probe loaded the complete Odoo registry; legacy
fiscal modules performed registry initialization, so the probe was replaced by a
PostgreSQL read-only transaction plus filesystem resolution. The second attempt revealed
a non-interactive login-shell quirk: `clear_console` from `.bash_logout` changed an
otherwise successful status to 1. The probe now uses a non-login container shell and
lets the remote login shell reach EOF naturally.

No failed attempt installed `meta_api_base`. Their evidence remains available beside the
canonical run for diagnostic traceability.

## Next release boundary

`contact_center_meta` will depend on `meta_api_base` and retain its existing
`services.graph` path as a compatibility facade. It must translate neutral errors to the
established Contact Center adapter errors without changing caller behavior. Only after
the complete Contact Center suite and a combined laboratory upgrade pass should the
duplicated transport implementation be removed.

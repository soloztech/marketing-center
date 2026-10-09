# E3 build — advisory handoff

Implementation remains uncommitted in this checkout. No commit, push, publication,
production access, CMS write, action archive, service/browser stop, credential change or
edit to an original/sibling checkout was performed. No permission/sandbox denial was
encountered. This report supports independent inspection; it does not certify native
Odoo, registry, browser, upgrade or production behavior.

## Frozen input and source

- Plan SHA256 verified:
  `43f57fba172d8838fddd998e88da03b07107e60c28724d4f99b87b2169591a0d`.
- Checkout/base: `marketing-center-e3`, `dad9e54b423a8953692c24ac327e8266c991dd45`. The
  tree was clean before implementation. This is the provisional E1 baseline, with no
  claim that it is deployed or finally inspected.
- Read repository AGENTS.md, E3-BUILD-NOTES.md and E3-BUILD-BASE.json.
- Candidate versions: Website `16.0.2.4.0`, Website WhatsApp `16.0.1.7.0`.
- Ownership: only Website, Website WhatsApp, `docs/`, and the factual Website Python
  namespace statement in root ARCHITECTURE.md. Soloz widgets, CMS recipes/driver,
  inventories of actual installed providers and all operational work remain owned by the
  coordinator.

- `marketing_center_website` baseline Git tree:
  `d2d085b57c67ab97987d3c55732767f0bdf1d10f`.
- `marketing_center_website/__manifest__.py`: baseline SHA256
  `12b8541c1ad7501a19cf8d90fed6ddf7c5180d2b3c0909d1c3d57cfcadd54833`; candidate SHA256
  `a5379cc1c017be8277c97b839333c74a4512abe1e531feb4b370ee73cb78b42c`.
- `marketing_center_website/models/crm/native_submission.py`: baseline SHA256
  `e1840f6ed406df7a3773bcbdab6c103516308c04b29e0a5ba4c684728cd59803`; candidate SHA256
  `c8d7328e06a8afb6eafee600363ca04ce571b2ef56d4394aee47365bb15913f5`.
- `marketing_center_website_whatsapp` baseline Git tree:
  `84651f0e6118e17af500712ed62daac8091ac8b8`.
- `marketing_center_website_whatsapp/__manifest__.py`: baseline SHA256
  `9a4bca6344ba41944f179969f007b6a3fa39dbd0a45aa7aa2bbc48dabd86901b`; candidate SHA256
  `d952e63a8fdaf10966ad7178e8e22519694cedf9b33339ea738e43085fdfb982`.
- `marketing_center_website_whatsapp/models/website_capture.py`: baseline SHA256
  `acc284d60ca2274dd9a4b128d61f56a517ee26311f25474496672f6dbe9d9730`; candidate SHA256
  `d3cf5813b6d2872d4c670948962a0b184093ee9680683e02a3f4a7d2fe289c5c`.

## Implemented behavior

The common acquisition resolver retains all eleven parameters and the R1 matrix. It
bounds history to 200 tracks in the preceding 24 hours for the same origin and Website,
orders by `(visit_datetime, id)`, and requires the effective-page anchor before
historical inference. Partial tuples do not mix cookie values. Cookie-only forms no
longer invent acquisition time from a page visit. The explicit `form` and `whatsapp`
purposes preserve the specified `none` difference. Acquisition time and visit time
remain separate; WhatsApp now persists the actual clicked-page anchor's visit time even
when the acquisition track precedes it. Existing direct internal callers without that
new optional argument retain their earlier behavior.

Current-namespace helpers `safe_page`, `acquisition_values`, and `utc_iso` remain
exported by native_submission.py. journey_ui.py is an existing consumer and remains
unchanged. Both retired-contract imports in WhatsApp use
`marketing_center_website.services.ingress.contracts`. Website's Python alias
registration is removed; Base helpers, XML IDs, fusion migrations and retired module
metadata remain preserved. A local Python AST scan found zero direct imports or literal
importlib consumers of the two retired namespaces; remaining textual hits were reviewed
as migration/XML metadata and negative tests. This does not prove absence of consumers
in the actual production installed providers.

With cookies_bar=False the UTM hook delegates immediately to OCB. The restricted
bar-enabled adapter stays in place. The shared no-store bootstrap envelope contains
schema_version=1, ingress and consent. It resolves one binding and calls the same
section functions as the two retained GETs. Ingress retains conditional HTTPS, including
HTTP eligibility for a configured non-consent legal basis; consent keeps mandatory
HTTPS. Native ingress configuration contains no key. Origin, public user and company
checks remain enforced, and POST admission still checks current policy.

The frontend shares one bootstrap Promise per generation. Decision/revocation and
cross-tab withdrawal invalidate old responses before readers can use them. A local
withdrawal fence denies individual capture even if a fresh GET precedes the POST.
Unknown/unavailable configuration does not erase attribution. No fabricated receipt or
policy-version/config-revision write was introduced. GA4 code is unchanged. Only an
explicit native HTML hint skips landing capture; absent/unknown hints retain legacy
bootstrap behavior on blog/jobs/thank-you paths. Native forms use a wrapper around
originalPost directly; the legacy form bridge is constructed lazily only under a valid
enabled legacy mode. Unknown mode uses originalPost. Native failures, UUID retries,
actual form confirmation, trusted legacy/WhatsApp activation and fail-open behavior
remain covered.

Archived WhatsApp references are accepted only for the validated actual page and a
strictly equivalent current effective rule: same Website/company/binding, kind,
destination, prefill, inbox and enabled capture. The archived rule must have belonged to
that page; active exceptions, disabled capture, wrong pages and differences still deny
admission. Resolution is repeated under the existing endpoint fence. New codes and
receipts use the effective action. Historical lookup is unchanged. HTTP retries check
the stored receipt before selecting acquisition, preserving frozen campaigns. No actions
were archived by this build. The four-character code, href prefill and global-link
behavior remain unchanged.

## Tests and proof limitations

Meaningful native tests were added for both consumers: all R1 provenance/anchor rows,
eleven fields, partial tuples, cookie-only next-day visits, 24-hour/200-track limits,
same-time ordering, future visits, other Websites/origins, and no-anchor inference. The
form recovery test exercises an E1-shaped frozen snapshot and fails if recovery calls
the resolver. WhatsApp tests cover archived old tabs, different prefill/page, disabled
capture and active exceptions; the replay test fails on reselection. A test separates
persisted visit_at from acquisition_at. UTM tests assert delegation before marketing
lookups and extended native tracking fields. HTTP tests compare bootstrap sections with
old GETs, including HTTP legal basis, auth/host/kill switches, refusal, malformed native
preference and absence of fictitious receipts. Current namespace imports and helper
exports replace virtual-alias tests. A required nonzero QUnit module covers shared
generation, refusal/stale responses, tri-state hints and the lazy adapter's
native/original/legacy dispatch.

The agreed command was run exactly:

```sh
python3 /home/lucaszotelli/.local/state/claudex-loop/marketing-platform-delivery-20261008/e3_static_proof.py /home/lucaszotelli/.local/state/claudex-loop/marketing-platform-delivery-20261008/marketing-center-e3
```

It exited 1 before inspecting any source:

```text
Traceback (most recent call last):
  File "/home/lucaszotelli/.local/state/claudex-loop/marketing-platform-delivery-20261008/e3_static_proof.py", line 5, in <module>
    assert repo.name=='contact-center-e2'
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
AssertionError
```

The coordinator-owned script was not edited, and no renamed/symlink checkout was used to
bypass its assertion. Proposed deviation: the coordinator corrects that assertion to
marketing-center-e3 and reruns the agreed command. In the meantime, supplemental local
checks performed git diff --check, ast.parse of changed Python, lxml parsing of changed
XML (none), and node --input-type=module --check of changed JS/MJS. These passed and are
only syntax evidence. Final output:

```text
LOCAL_E3_SYNTAX_OK 41 {'py': 23, 'xml': 0, 'js': 13} Native and real-browser verification remains coordinator-owned.
```

Seven Node runners passed (no real leads, external APIs or WhatsApp sends):

```sh
node marketing_center_website/tests/bootstrap_node.mjs
node marketing_center_website/tests/native_form_node.mjs
node marketing_center_website/tests/landing_withdrawal_node.mjs
node marketing_center_website/tests/consent_race_node.mjs
node marketing_center_website/tests/consent_domain_cookie_node.mjs
node marketing_center_website/tests/action_navigation_node.mjs
node marketing_center_website_whatsapp/static/tests/whatsapp_handoff_node.mjs
```

Their output:

```text
Shared bootstrap: one fetch; stale/refused generation denied; tri-state native skip; no-hint blog/jobs/thank-you compatibility; unavailable fail-closed.
Native Website JS: no landing/session/exchange; stable retry token; confirmed form event; native body/result/failure preserved.
Landing withdrawal tests: stale config and stale accepted response cannot recreate optional storage.
Consent race tests passed: stock choice bridge, informational attribution, refusal and restoration.
Consent cookie scopes: all three host/Domain UTMs erased; required cookies and other domains preserved.
Action navigation tests: delayed exchange/callback precede navigation; bounded failures preserve native results; invalid forms and thank-you visits do not convert.
WhatsApp handoff JS: independent page/Website config, thank-you without GA/forms, editorial and floating CTAs preserved, fixed destination, stable retry UUID, bounded stalled body, fail-open, keyboard, popup, modifier/editor/policy behavior passed.
```

Native/HTTP suites, cold registry of the complete installed composition, native
upgrade/inverse, real QUnit normal/debug, Playwright, CMS dry-run/apply/inverse and
production verification were not run here. They remain coordinator-owned promotion
gates. No Docker, native QA script, outside-worktree proof write or browser session was
attempted. There is no claimed browser or database equivalence evidence. The final E1/E2
baseline must be revalidated, rebased, version bumps recomputed and affected proofs
rerun before independent final inspection and any promotion.

## Separately identifiable alias retirement

For a separate coordinator commit, stage the alias changes in these files/hunks:

- marketing_center_website/**init**.py: remove both alias registrations/imports.
- marketing_center_website/ingress_aliases.py: delete the virtual alias adapter.
- marketing_center_website/tests/test_legacy_imports.py: current namespaces and absence
  of retired registration (helper-export test can follow acquisition).
- marketing_center_website_whatsapp/controllers/website_handoff.py: retired import
  replacement only; the other hunks belong to acquisition/archived-tab behavior.
- marketing_center_website_whatsapp/models/website_action.py: retired import replacement
  only; the other hunks implement strict archived-tab resolution.
- ARCHITECTURE.md: factual Website Python namespace statement only.
- docs/web-ingress-fusion.md and docs/crm-integration-fusion.md: factual namespace
  statements, preserving XML metadata and other providers' aliases.

No commit was created. Actual installed-provider executable-consumer clearance is a
coordinator prerequisite before merging alias retirement.

## Complete changed-file inventory

M = modified tracked file; D = deleted tracked file; N = new untracked file. All changes
are inside the owned paths. This inventory includes this report.

- M `ARCHITECTURE.md`
- M `docs/crm-integration-fusion.md`
- N `docs/e3-build-report.md`
- M `docs/web-ingress-fusion.md`
- M `marketing_center_website/__init__.py`
- M `marketing_center_website/__manifest__.py`
- M `marketing_center_website/controllers/__init__.py`
- N `marketing_center_website/controllers/website_bootstrap.py`
- M `marketing_center_website/controllers/website_consent.py`
- M `marketing_center_website/controllers/website_ingress.py`
- D `marketing_center_website/ingress_aliases.py`
- M `marketing_center_website/models/crm/native_submission.py`
- M `marketing_center_website/models/ir_http.py`
- M `marketing_center_website/services/__init__.py`
- N `marketing_center_website/services/acquisition.py`
- M `marketing_center_website/static/src/js/action_bootstrap.esm.js`
- M `marketing_center_website/static/src/js/action_capture.esm.js`
- N `marketing_center_website/static/src/js/bootstrap_config.esm.js`
- M `marketing_center_website/static/src/js/consent.esm.js`
- M `marketing_center_website/static/src/js/landing_bootstrap.esm.js`
- N `marketing_center_website/static/tests/bootstrap_config_tests.esm.js`
- N `marketing_center_website/tests/acquisition_cases.py`
- M `marketing_center_website/tests/action_navigation_node.mjs`
- N `marketing_center_website/tests/bootstrap_fixture_node.mjs`
- N `marketing_center_website/tests/bootstrap_node.mjs`
- M `marketing_center_website/tests/consent_domain_cookie_node.mjs`
- M `marketing_center_website/tests/consent_race_node.mjs`
- M `marketing_center_website/tests/landing_withdrawal_node.mjs`
- M `marketing_center_website/tests/native_form_node.mjs`
- M `marketing_center_website/tests/test_controller.py`
- M `marketing_center_website/tests/test_js.py`
- M `marketing_center_website/tests/test_legacy_imports.py`
- M `marketing_center_website/tests/test_native_submission.py`
- M `marketing_center_website/tests/test_native_utm_policy.py`
- M `marketing_center_website_whatsapp/__manifest__.py`
- M `marketing_center_website_whatsapp/controllers/website_handoff.py`
- M `marketing_center_website_whatsapp/models/handoff.py`
- M `marketing_center_website_whatsapp/models/journey_capture.py`
- M `marketing_center_website_whatsapp/models/website_action.py`
- M `marketing_center_website_whatsapp/models/website_capture.py`
- M `marketing_center_website_whatsapp/tests/test_website_handoff.py`

Total: 41 files.

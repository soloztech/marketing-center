# Web ingress owned by Website

`marketing_center_web_ingress` is folded into `marketing_center_website` 16.0.2.1.0. The
repository has 13 addons. `marketing_center_website_whatsapp` stays separate because it
integrates native Contact Center CRM.

The provider-neutral parser, ledger, vault, admission/rate limits and generic
`/marketing/web-ingress/<public_ref>` route remain intact in `models/ingress`,
`services/ingress` and `controllers/ingress`. Website overlays load after the core.
Historical Python module paths point to these same loaded objects. A generic ingress
client now requires Website to be installed.

Supported upgrade predecessor: ingress 16.0.1.5.0 and Website16.0.2.0.0, already
installed. Standalone-ingress databases must install the predecessor Website before
changing source. The install hook refuses unadopted ingress rather than duplicating
data. Fresh installs need only the new Website addon.

Under writer exclusion, call `ingress_migration.prepare(env)` with the predecessor
registry. It validates the exact legacy catalog (141 XMLIDs,34 constraints), creates
missing canonical aliases with original update policy, freezes historical aliases,
transfers schema ownership and retires the old module as uninstallable. Website's
ingress dependency is repointed in-place to explicit `web`, already a native
prerequisite used by its assets/controllers. Historical inactive dependency rows stay
intact. No native uninstall of ingress occurs.

Persist the receipt before ACK/commit and source promotion. Upgrade Website through the
normal native loader; its dependent WhatsApp is included. The2.1 pre-migration refuses
unprepared metadata; post-migration writes
`marketing_center.ingress_fusion.marketing_center_website` before native data commit.
Historic CRM stage markers retain their original values; the Base read-only guard
accepts the new Website lineage.

Inverse is supported only before any new data checkpoint, with no pending module flags
and an exact prepared receipt. Committed/uncertain failures retain new source and stay
stopped for repair. No automatic retry or database restore. Actual Website uninstall
removes the old ingress aliases/copies and old CRM aliases before native cleanup;
retiring ingress never invokes that uninstall path.

URLs, data IDs, protected click values, retention/settings/ACL, deduplication, native
form/lead creation and WhatsApp correlation are unchanged. This is packaging/ownership
simplification, not a new attribution or automatic lead policy. Complete source at
official16.0, proportional native QA and a production rollback dry-run are required
before deployment.

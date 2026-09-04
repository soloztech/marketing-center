Marketing Center - Website
==========================

This addon is a first-party Odoo Website adapter for
``marketing_center_web_ingress``. It does not create another attribution
ledger and does not submit website form values to Marketing Center.

Architecture
------------

* One website has at most one binding, and an active binding points to one
  active ingress endpoint in the same company.
* ``GET /marketing/website-ingress/config`` resolves the current website and
  returns only ``enabled``, the relative ingress path, its browser-visible
  public routing key and the current configuration revision. The GET route
  creates no event or touchpoint.  Every landing POST must echo that revision;
  a security/configuration change revokes a stale page immediately.
* A small frontend asset submits one ``entry_point`` envelope per browser
  session. Browser idempotency is best effort; Web Ingress remains the
  authoritative deduplication and persistence boundary. A ``409`` closes the
  browser retry because it proves that the first-wins ingress already owns that
  event identifier.
* Authenticated sessions and Odoo technical/login/portal paths are excluded.
* The browser reads only the landing origin/path, the referrer origin, and
  the explicit ``utm_*``, ``gclid``, ``gbraid``, ``wbraid`` and ``fbclid``
  allowlist. URL fragments, other query parameters, cookies, form controls,
  form values and DOM text are never read or sent.
* Opaque event and session UUIDs contain no partner, user or browser identity.
  No persistent visitor identifier is created by this addon.
* A configured native Website form emits ``form_submission`` only after Odoo
  has successfully created its record. The bridge passes the native payload
  through unchanged and exchanges only a short-lived HMAC receipt plus opaque
  action/event/session UUIDs.
* A configured WhatsApp button emits ``organic_link`` after a trusted visible
  activation. Only then does the server return an opaque, expiring, one-use
  redirect grant. The destination is fixed in server configuration and is never
  accepted from browser input. This dedicated handoff is not a generic link-click
  collector.
* Landing, form and handoff actions share the same opaque session hash. UTM and
  click identifiers remain on landing evidence and are not recopied from the
  page when a later action occurs.
* The shared Web Ingress event records ``browser_capability`` for landing
  observations and ``website_confirmed_action`` for form/handoff actions.  The
  public generic route cannot manufacture the latter provenance.

Native-first click ownership
----------------------------

``link_tracker`` remains optional and is deliberately not a dependency of this
adapter. When an Odoo-managed link is eligible for native tracking (including
native mailing and SMS trace descendants below ``/r/<code>``),
``link.tracker.click`` owns that click fact and Marketing Center must not create a
second canonical click or ``organic_link`` for the same activation. The Website
adapter observes only its explicitly marked form and WhatsApp handoff controls;
ordinary and shortened links stay with Odoo.

The native click model has its own deduplication semantics, so neither this addon
nor its documentation promises one database row for every physical click. Any
future correlation with native clicks belongs in an optional bridge and must
reference the native fact instead of duplicating it.

Likewise, this adapter records textual UTM evidence in the provider-neutral
touchpoint but does not create a competing campaign/source/medium catalog.
Resolution to Odoo's native ``utm.campaign``, ``utm.source`` and ``utm.medium``
belongs to the optional native bridge and its explicit first-trusted/fill-only
policy.

Configuration
-------------

Create the Web Ingress endpoint first. Its exact origin and landing-host
allowlists must include the website. Then open *Marketing Center >
Configuration > Website Ingress* and bind the website to that endpoint.

Create each tracked form or WhatsApp handoff under *Marketing Center >
Configuration > Website Actions*. An action stores an immutable technical
route, exact queryless source path and either an Odoo model enabled for Website
forms or a fixed WhatsApp E.164 destination. Copy its generated marker onto the
corresponding native form or link:

* ``data-marketing-form-action="<opaque UUID>"`` on the form or send control;
* ``data-marketing-whatsapp-action="<opaque UUID>"`` on a link whose ordinary
  ``href`` is a safe local fallback such as ``/contactus``.

Action identity and destinations cannot be edited after creation. Archive and
replace an action when its meaning changes so old touchpoints keep a stable
technical reference.

The public key is deliberately visible to browsers. It is a revocable routing
capability, not authentication. Exact same-origin landing validation,
configuration-revision fencing, bounded envelopes, replay fencing and
transactional deduplication are enforced by Web Ingress.

Security and operational notes
------------------------------

Bindings are available only to Marketing Administrators and are fenced by the
active-company record rule. Active endpoints cannot be archived while a live
website binding references them.  Website, endpoint, binding and action changes
use a common endpoint-first MVCC fence.  This is intentionally stronger than a
lock-only check under Odoo/PostgreSQL ``REPEATABLE READ``: a concurrent stale
configuration transaction is retried instead of committing an invalid
combination. The configuration response has ``no-store`` and no CORS opt-in.

Action routes additionally require the exact current Origin, reject prefetch
headers, accept strict JSON bodies of at most 2 KiB and do not serve
authenticated sessions. Browser ``isTrusted``/user-activation checks exclude
prefetch and synthetic DOM dispatch, but cannot distinguish advanced browser
automation. Reverse-proxy rate limits and native form reCAPTCHA remain defense
in depth.  The database safety net applies separate per-endpoint request classes
for forms and WhatsApp handoffs and returns an explicit ``429`` when full.
Admission happens before action/HMAC resolution, so a syntactically valid but
unknown/tampered public claim cannot bypass the safety net by intentionally
failing late validation. Strict body-size and JSON parsing remain the earlier,
payload-free rejection boundary.
Redirect grants store only token/event hashes, permit at most one issued grant
per action/event and are purged in bounded batches after their short retention;
they are transport capabilities, not attribution evidence. Their lifecycle is
one-way (``issued`` to ``consumed`` or ``revoked``) in both ORM and database
constraints. Grant lookup is
scoped to the resolved Website and company before any cross-model row is locked.

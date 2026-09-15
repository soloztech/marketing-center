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

Native UTM cookies on informational landing pages
------------------------------------------------

For public HTTPS GET requests to a published page with one active CRM form
action, an active same-company binding and an eligible informational policy,
the adapter lets Odoo's native ``ir.http._set_utm`` persist its three standard
UTM cookies. Odoo retains the cookie values, lifetime and later CRM defaults.
An existing native optional-cookie choice retains native behavior, including refusal;
paused capture, individual consent, other pages and extended tracking fields
receive no exception. The temporary permission ends with the native UTM writer:
other optional cookies remain subject to Odoo's normal decision. The adapter
does not create consent, modify the native preference cookie or write CRM UTMs.

Configuration
-------------

Create the Web Ingress endpoint first. Its exact origin and landing-host
allowlists must include the website. Then open *Marketing Center >
Configuration > Website Ingress* and bind the website to that endpoint.

Bindings alone do not enable optional tracking. A Marketing Administrator must
explicitly configure the purpose, policy/notice versions, justification and a
retention mode. The default remains blocked. ``duration`` requires a positive
number of days; ``manual`` is an explicit decision to preserve attribution
history until an operator requests deletion. Zero days alone never means manual
retention. Each event and CRM intent snapshots that decision; existing legacy
rows do not inherit it silently.

When the endpoint legal basis is ``consent``, the native Website cookie bar is
connected to a dedicated individual-decision producer. Its explicit same-origin
JSON request must agree with the native optional preference and current policy
versions. The server stores an opaque, website/company/endpoint-scoped decision
without IP address, user agent, form values or campaign values. A signed,
host-only Secure/HttpOnly/SameSite=Strict cookie refers to that decision. The
browser-visible routing key and a JavaScript boolean never authorize capture.

GET ``/marketing/website-consent/config`` writes no decision. Every capture and
form receipt checks the current decision; public capture/action requests also
echo the decision reference from their page configuration, so stale pages cannot
reuse a later decision. Revocation invalidates old cookies and receipts under a
transactional lock, including pending CRM recovery. Native forms and direct
WhatsApp links keep working when tracking is refused. Withdrawing consent stops
new attribution and does not delete historical leads or existing correlations.

``consent_ttl_days`` controls only the receipt's technical validity (default 999
days, matching Odoo's native banner), independently of history retention. Daily
bounded maintenance erases expired receipt identifiers but preserves the minimal
policy audit and historical intent references. Finite event/intent deadlines
keep their original cleanup behavior; explicitly manual history has no automatic
purge deadline.

Frontend integration exports ``loadConsent()`` from
``@marketing_center_website/js/consent.esm`` and emits document events
``marketing_center:consent-ready`` after a server GET and
``marketing_center:consent-changed`` after a decision. Consumers may grant only
``ready.granted === true`` or ``changed.granted === true`` with
``confirmed === true``. A false value must disable optional measurement at once.
A control marked ``data-marketing-consent-revoke`` withdraws the decision and
reopens the native banner. The event ``marketing_center:action-confirmed``
contains ``kind`` and opaque ``event_id`` after a successful server exchange.
Listeners may synchronously register ``detail.waitUntil(promise)`` for a bounded
analytics callback. A successful native form waits at most two seconds for its
exchange and callbacks before returning the exact native result; tracking errors
cannot fail the form. Callbacks alone are capped at 750 ms. A thank-you page visit
never emits a form success event.
Only ``form_submission`` signifies a native form success; ``whatsapp_handoff`` is
navigation, never a lead or sale. This addon sends no external analytics itself.

When policy is disabled, the config response contains only ``enabled=false``;
the browser does not prepare optional landing/action identifiers from that
configuration. The server independently enforces the gate on stale clients.
Native Website forms continue their usual record creation without a marketing
receipt or correlation. Ordinary links and the configured fallback navigation
remain native browser behavior.

Create each tracked form or WhatsApp handoff under *Marketing Center >
Configuration > Website Actions*. An action stores an immutable technical
route, exact queryless source path and either an Odoo model enabled for Website
forms or a fixed WhatsApp E.164 destination and optional fixed public CTA message.
The server URL-encodes that immutable message; browser text cannot override it.
Copy its generated marker onto the
corresponding native form or link:

* ``data-marketing-form-action="<opaque UUID>"`` on the form or send control;
* ``data-marketing-whatsapp-action="<opaque UUID>"`` on a link whose ordinary
  ``href`` is a safe local fallback or strict HTTPS ``wa.me/<E164>`` URL. The real
  link stays usable when optional tracking is refused.

Action identity and destinations cannot be edited after creation. Archive and
replace an action when its meaning changes so old touchpoints keep a stable
technical reference.

The public key is deliberately visible to browsers. It is a revocable routing
capability, not authentication. Exact same-origin landing validation,
configuration-revision fencing, bounded envelopes, replay fencing and
transactional deduplication are enforced by Web Ingress.

Security and operational notes
------------------------------

Permanent informational Website policy
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Since 16.0.1.4.0, ``website_tracking_policy`` is a persistent endpoint setting.
Its default ``individual_consent`` keeps the existing individual consent rules.
The explicitly selected ``informational_notice`` policy allows documented
operator-controlled capture without representing a visitor grant. It is part
of the audited policy field set: changes increment ``config_revision`` and
record the responsible administrator and timestamp. Purpose, policy and notice
versions, justification, retention, capture enablement, host/company, anonymous
session, HTTPS, technical-page and confirmed-event guards still apply.

The Website cookie bar is rendered with one informational text and a Prosseguir
button. Native choice anchors remain in XML for inherited view compatibility,
but are not rendered as HTML under this policy. The guard exists before the
native Odoo popup initializes; no failed/delayed consent request, paused capture,
old native preference or navigation to login/404 restores choice buttons.
Prosseguir stores only a dismissal in localStorage, scoped to Website and notice
content. It creates no native optional-cookie choice or consent receipt. Storage
failure keeps dismissal in the current page. The notice works without GA4 and
without a public measurement configuration on the current route.

New evidence records ``consent_state=unknown``,
``decision_source=operator.website_notice`` and
``web_ingress.website_tracking_policy=informational_notice``. The migration
clears an old consent legal-basis label rather than claiming a different legal
basis. Historical receipts and evidence are not modified or reclassified.
Public configuration exposes ``informational_notice`` and ``capture_allowed``
separately from the actual ``granted`` decision. Capturing under this policy
requires both server flags. It never depends on clicking Prosseguir.

Upgrade / explicit migration contract
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The old global parameter ``marketing_center_website.tracking_test_endpoint_ids``
is no longer read. An ordinary module upgrade never promotes its IDs to the
permanent policy. Before upgrading, the release operator snapshots both old
Website text fields in every installed language (including ``en_US``), then
copies the chosen old ``marketing_cookie_test_notice_text`` into
``marketing_cookie_notice_text`` with the old registry's ORM. This preserves
custom text and translations when the old field is removed by native upgrade.
The settings retain just one text, the button label and privacy URL.

After upgrading, use the private ORM helper on the exactly reviewed endpoint::

    endpoint._activate_informational_notice(
        website_id=reviewed_website_id,
        binding_id=reviewed_binding_id,
        company_id=reviewed_company_id,
        expected_revision=reviewed_revision,
        policy_version=new_policy_version,
        notice_version=new_notice_version,
        justification=reviewed_operator_instruction,
    )

The helper fences configuration rows, validates exact positive IDs, company,
active binding/endpoint, enabled native cookie bar, expected revision and new
policy/notice versions. It writes the policy through the normal audited ORM
path and returns the resulting identities/revision. No consent history is
changed. Remove the obsolete parameter only in the reviewed release operation,
after comparing its exact old value. Preserve snapshots and rollback evidence.
No public request, context boolean, stale test parameter or default module
installation can invoke this activation.

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

Website notice and optional Google measurement
---------------------------------------------

Since 16.0.1.3.0 this adapter also owns the native cookie-bar integration and
the optional GA4 consumer of confirmed Marketing events. It needs neither
``marketing_center_website_crm`` nor any company-specific Website addon. The
CRM bridge is still required when correlating actual native CRM submissions.

Website → Configuration → Settings → Privacy exposes one notice text, the
continue-button label and policy URL for the selected Website. Values are
escaped as plain text. Editing them does not grant consent or change endpoint
policy/revisions. The informational bar works independently of GA4.

GA4 reads the native Website ``google_analytics_key``. A configured Marketing
binding suppresses both native Google scripts, even when paused, so pausing
capture cannot accidentally revive another loader. Unmanaged Websites retain
their native behavior. The public configuration is rendered outside QWeb's
shared cache and is restricted to the active Website/company, HTTPS host,
anonymous user and published unrestricted page. Server consent/capture checks
remain authoritative; DOM metadata is not an authorization capability.

The optional consumer sends one page view, ``generate_lead`` after a confirmed
form receipt and ``whatsapp_handoff`` after a confirmed handoff. It does not
read form values or treat a thank-you page as proof. Google query/referrer data
is filtered more narrowly than the first-party ingress, and advertising
storage/user data/personalization remain denied. Keep automatic Google form
tracking and duplicate Google/GTM loaders disabled for this event contract.

Existing explicit action markers are preserved. Automatic form annotation
requires exactly one eligible native CRM form and one configured action.
WhatsApp annotation requires a SHA-256 match of the existing public link's
destination and message against the configured action; special document-request
messages continue normally instead of being replaced by a generic handoff.
Administrative destinations/messages are not advertised in configuration.

Website to WhatsApp attribution
===============================

This optional bridge adds a reference to configured WhatsApp links and relates
incoming Contact Center messages to native Website visits. It depends on
``marketing_center_website``, ``contact_center_crm``,
``marketing_center_contact_center`` and ``queue_job``. It does not replace
native tracking, create a parallel visitor session, send messages or merge leads.

Configuration
-------------

On an existing Website WhatsApp action, enable conversation linking and select
the exact Contact Center account. New clicks receive a four-character code;
legacy prefix settings remain stored but do not lengthen new codes.
The account must be active, belong to the Website company, and declare the same
destination phone as its own identity. Existing actions remain disabled on
installation. On Website Settings, select an enabled action from that Website as
``Regra padrão do WhatsApp``. This reuses its destination and conversation routing
on every current or future public page without creating a rule for each URL.
An existing active page action takes precedence, including its disabled linking
setting; ambiguous page rules fail closed. Archived page actions are ignored.
Clearing the Website default leaves existing page-specific rules in place.
An action belonging to another Website/company cannot be used as the default.

The same resolver validates both the rendered page and the click request.
Only published public ``website.page`` records in native mode are eligible;
technical routes, private pages and authenticated/editor sessions are excluded.
The real clicked page is recorded even when it uses the common default action,
and an event UUID cannot be replayed on another page. A published thank-you page
can use this WhatsApp setting without adding native form measurement: the addon
renders its own minimal configuration. Native Google pageviews follow Website
settings independently, including on thank-you pages.

Links must point to the configured destination number; their original messages
are preserved, including document requests and floating buttons. The browser
adds the server-issued reference without sending the editorial text to the
capture endpoint. Links to other numbers retain their own behavior.
Policy enforcement reuses the existing Website
capture setting and consent receipt, including its informational mode.

Click and reference
-------------------

An ordinary click or keyboard activation records the native visitor, page,
server time, account, and available UTM/click identifiers. The snapshot follows
the page's prior native tracks (up to 24 hours and 200 records) and never fills
a new click tuple from stale campaign cookies. Context is frozen at the click.
The visible reference contains four random uppercase consonants/digits (29 symbols, 707281 possible codes), without a
prefix. Codes are attribution evidence, not identity or authentication. The code is appended on one new line as ``Meu código é WX2Y``.
Database uniqueness and bounded retry
prevent a repeated code from identifying another click. No IP, phone, visitor
ID or campaign IDs are encoded in the reference. Previously issued long
references remain recognizable; retries of those clicks keep their old format.
The message before the code still comes from the clicked link's ``text``
parameter; the configured action text does not replace that editorial content.

The 707281-code space is a lifetime limit: codes are not recycled after the
30-day matching window and the unique constraint covers every company/Website.
This deliberately preserves historical evidence without assigning an old code
to another click. New capture fails open to the original WhatsApp link if all
20 allocation attempts collide; it never deletes evidence to free a code.
Operators must check global compact-code occupancy in the native Odoo shell:
``env['marketing.website.whatsapp.handoff'].sudo().search_count([('reference', '=like', '____')])``.
Investigate growth at 10 percent and plan capacity/retention work before 25 percent;
disable ``handoff_enabled`` to stop new allocations during an incident while
keeping conversations and legacy associations usable. A sustained unauthenticated
flood can consume this finite space despite the existing endpoint/edge limits.
Any recycling or purge needs a separate reviewed retention migration; never
manually remove attribution records as an exhaustion workaround.

Retries reuse the reference; repeated activations with the same session, action,
page and acquisition within 60 seconds normally share a click; concurrent requests
with different event IDs can still record separate clicks. The server validates the
fixed destination and constructs the prefilled WhatsApp message. Network failure,
blocked popups and unavailable capture preserve the original link. Modified and
middle clicks keep native browser navigation and do not receive a reference.

An exact reference in an original inbound direct message can associate a click
within the preceding 30 days, in the same account and company. It does not prove
that a forwarded/copied code belongs to the original person. Provider-marked
forwarded messages and replies are excluded. Unknown, partial or multiple codes
do not silently become temporal matches. Compact codes require the explicit
``Meu código é`` label (ASCII spelling is also accepted), so ordinary
four-character words do not become reference matches. No historical messages
are backfilled.

Suggestions and review
----------------------

Without a code, only the first inbound message or a return after 24 hours without
conversation activity receives temporal candidates. The preceding 10-minute
window uses the provider's message time, with up to two seconds for timestamp
rounding; delayed webhook delivery does not move the event time. Up to five
candidates are shown; overflow is explicitly flagged. The score only orders
proximity and is not a calibrated probability. These windows are initial rules,
not measured conversion accuracy.

Marketing Center → Site → WhatsApp → Associações shows the reference, source page,
message time, interval, competing candidates, state and evidence. Operators with
conversation access can confirm or discard a suggestion, or undo an association.
One click and one message can each have only one confirmed association. Reviews
are attributed to a user and time. The conversation form shows all associations;
the lead form shows confirmed origins through existing explicit CRM links. The
addon never chooses a lead by fuzzy phone/name matching or creates a new lead.

New captures freeze acquisition, visit, click and policy evidence. A received
code or human-confirmed association asynchronously projects one first-party
``website.whatsapp`` touchpoint, including before any business is linked.
Separate paged match and business-link workers converge retries and revocations.
Business credit follows the proving message's UTC time within a confirmed
half-open period; acquisition time is preserved. Editing or deleting the received
message later preserves the frozen claim; explicit rejection retracts its credit.
Global company-less leads remain context: this Website authority does not anchor
their Marketing company or assert credit. Existing Base flows may already have
anchored that company. Old captures are read-only
and never backfilled. Advertising conversion feedback and scheduled sending
are not introduced. Temporal suggestions stay distinct from reference and manual
associations. Measuring their accuracy against reviewed reference pairs remains
necessary before considering automatic inference.

Visitor Journey and privacy
---------------------------

The visitor form's Journey requires native visitor read permission and Contact
Center administration. It lists visits even without a click, local campaign
catalog resolution, clicks, authorized conversations and paged visible businesses.
Catalog ambiguity and absence are explicit; a URL campaign ID does not prove the
exact ad. Business Journey offers authorized evidence navigation. Every action
rechecks the current relation and access; payloads omit raw IP, click IDs and
session values. Native login and partner merge move handoffs only within the same
Website/company. Native deletion preserves frozen evidence and marks missing
visitor/track references.
Historical handoffs can be erased or detached after the Website changes company;
creation-time topology is not revalidated by navigation cleanup. Optional visitor
cleanup is isolated from native login/unlink, while concurrency errors still retry.

Only explicitly linked business conversations expose their private Website acquisition
snapshot; other customer conversations retain their ordinary context only.
The authorized business Website chain remains visible to its Contact/CRM agent
without Marketing administration or visitor rights; catalog and other Marketing
origins keep their own access checks. Acquisition fallback and provenance are
explicit. Visits trust the active binding's allowed origins, falling back to the
Website domain; missing host configuration is shown as such.

Possible other accesses compare only the latest valid IP observations on the same
Website within 24 hours, ordered by time distance. This read-only area does not
merge identities, visits or credit; a shared network is not a person.

The existing retention cron also erases expired P2 handoffs and all canonical
private free-form copies (URLs, UTMs and click identifiers) in bounded batches.
The existing Base erasure contract preserves technical campaign IDs/providers,
asset-resolution projections and audit metadata. They remain reachable through
authorized technical records, but the Journey hides the campaign and the erased
origin cannot grant business credit. Failed rows rotate behind rows attempted fewer
times using an internal failure counter, so later expired captures can progress.
Explicit HTTP refusal queues early erasure
for the cookie-identified decision only; superseded decisions follow retention.
Each failed item is isolated; successful siblings commit. Five delayed retries
restart from cursor zero with batches of at most 100. Persistent failures remain
visible as native Failed jobs for operator repair and requeue, including manual
retention captures. The browser discards its consent cookie on refusal; repeating
the browser refusal is not the recovery mechanism.
Receipt expiry or capture pause after projection does not revoke historical
credit. Erasure is asynchronous and irreversible to retries; only this bridge's
assertions are revoked, preserving independent authorities and manual UTMs.

Validation and reversal
-----------------------

Odoo tests cover HTTP capture, native visitor/UTMs, destination and company
boundaries, policy grants/revocation, idempotency, reference matching, ambiguous
time candidates, permissions, review and absence of sending/CRM side effects.
The Node regression exercises timeout, fallback, fixed target, keyboard and
popup behavior. Run ``node static/tests/whatsapp_handoff_node.mjs`` from the
addon directory, and Odoo with ``--test-tags /marketing_center_website_whatsapp``.
Pure parsing/formatting checks can also run locally with
``python3 tools/test_whatsapp_references.py`` from the repository root.

Disable ``handoff_enabled`` to stop new captures without affecting the original
links or native tracking. Existing evidence remains reviewable. No uninstall or
data deletion is required to turn off the feature.

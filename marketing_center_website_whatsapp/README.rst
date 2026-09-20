Website to WhatsApp attribution
===============================

This optional bridge adds a reference to configured WhatsApp links and relates
incoming Contact Center messages to native Website visits. It depends only on
``marketing_center_website_crm`` and ``contact_center_crm``. It does not replace
native tracking, create a parallel visitor session, send messages or merge leads.

Configuration
-------------

On an existing Website WhatsApp action, enable conversation linking, select the
exact Contact Center account and choose a 2–8 character uppercase prefix.
The account must be active, belong to the Website company, and declare the same
destination phone as its own identity. Existing actions remain disabled on
installation. On Website Settings, select an enabled action from that Website as
``Regra padrão do WhatsApp``. This reuses its destination and reference prefix
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
can use this WhatsApp setting without enabling GA4 or native form measurement on
that page: the addon renders its own minimal configuration.

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
The visible reference contains a readable prefix and 12 random characters.
No IP, phone, visitor ID or campaign IDs are encoded in the reference.

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
do not silently become temporal matches. No historical messages are backfilled.

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

No touchpoint projection, advertising conversion feedback or scheduled sending
is introduced. Temporal suggestions stay distinct from reference and manual
associations. Measuring their accuracy against reviewed reference pairs remains
necessary before considering automatic inference.

Validation and reversal
-----------------------

Odoo tests cover HTTP capture, native visitor/UTMs, destination and company
boundaries, policy grants/revocation, idempotency, reference matching, ambiguous
time candidates, permissions, review and absence of sending/CRM side effects.
The Node regression exercises timeout, fallback, fixed target, keyboard and
popup behavior. Run ``node static/tests/whatsapp_handoff_node.mjs`` from the
addon directory, and Odoo with ``--test-tags /marketing_center_website_whatsapp``.

Disable ``handoff_enabled`` to stop new captures without affecting the original
links or native tracking. Existing evidence remains reviewable. No uninstall or
data deletion is required to turn off the feature.

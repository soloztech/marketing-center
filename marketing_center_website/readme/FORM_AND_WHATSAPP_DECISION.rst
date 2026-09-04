Website form success and WhatsApp handoff
==========================================

Odoo 16's native Website form controller owns validation, reCAPTCHA, field
extraction, attachment handling and record creation.  Its frontend serializes
the form and does not emit a stable success event.  This adapter therefore
keeps the native implementation intact and observes only its technical edge:

* a narrowly scoped ``web.ajax.post`` bridge passes the native form body and
  promise through unchanged, and never reads form controls or values;
* three opaque references are added to the form URL only after a trusted user
  action, then removed from ``request.params`` before the native controller can
  process them;
* a successful native response receives a short-lived HMAC receipt.  Exchanging
  that receipt creates a provider-neutral ``form_submission`` through Web
  Ingress; it does not create a second ledger;
* a trusted WhatsApp activation first creates an ``organic_link`` through the
  same ingress.  The server then returns a short-lived, opaque, one-use redirect
  grant whose destination is selected only from immutable server configuration.

The WhatsApp action is a dedicated handoff, not a generic click collector.
``link_tracker`` remains optional.  If an Odoo-managed link is eligible for native
tracking, ``link.tracker.click`` owns that fact and this adapter must not create a
second canonical click or ``organic_link`` for the same activation.  Odoo's native
deduplication is accepted as-is; there is deliberately no promise of one row for
every physical click.

The browser reads only opaque action/session/event references and technical
markers.  It never reads form values, DOM text, cookies, phone numbers or
arbitrary redirect targets.  ``isTrusted`` and user-activation checks exclude
browser prefetch and synthetic DOM events; they cannot distinguish advanced
browser automation, which remains a rate-limit/reCAPTCHA concern.

Why the narrow AJAX bridge remains
----------------------------------

Odoo 16's native Website form widget invokes ``web.ajax.post`` internally and
does not expose a stable public success hook carrying its response.  Removing
the bridge would therefore either lose confirmed-form evidence or require a
fork of the complete native form widget.  The adapter wraps only the exact
same-origin ``/website/form/<model>`` call, forwards the original opaque body and
promise unchanged, and observes only the added receipt in a successful result.
All unrelated AJAX calls follow the original function byte-for-byte.

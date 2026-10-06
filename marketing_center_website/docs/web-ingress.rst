Marketing Center - Web Ingress
==============================

Provider-neutral, first-party web attribution capture for Marketing Center.
The addon depends on ``marketing_center_base`` only; it does not require Odoo
Website and does not ship browser JavaScript.

Contract
--------

Create an endpoint with explicit origins and landing hosts. POST a JSON object
to ``/marketing/web-ingress/<endpoint public_ref>`` with the endpoint's current
public key in ``X-Marketing-Ingress-Key``, its current configuration revision in
``X-Marketing-Ingress-Revision`` and the browser's exact ``Origin``.  The
revision is a revocation fence: any endpoint security change invalidates stale
browser configuration even when the public key itself was not rotated.

The public HTTP route accepts only ``entry_point``.  ``form_submission`` and
``organic_link`` use the same server-side contract and ledger, but may enter
only through a trusted internal adapter such as ``marketing_center_website``
after that adapter has proved its action-specific precondition.  This prevents
a browser-visible routing key from upgrading a claimed click into a confirmed
form success.

The body is bounded to at most 16 KiB and the shared server contract accepts
only:

* ``event_id``, ``event_type`` (``entry_point``, ``form_submission`` or
  ``organic_link``), and a timezone-aware ``occurred_at``;
* ``landing_url``, optional ``referrer_url`` and the five standard UTM values;
* ``gclid``, ``gbraid``, ``wbraid`` and ``fbclid``;
* optional opaque ``session_ref``/``visitor_ref`` and ``consent_state``;
* technical, opaque ``action_ref`` and ``route_ref`` for action events, plus
  ``model_ref`` only for successful form submissions.

The envelope remains capped at 20 fields. Event-specific semantics are strict:
``entry_point`` rejects every action-reference field; ``form_submission``
requires ``action_ref``, ``route_ref`` and ``model_ref``; ``organic_link``
requires the first two and rejects ``model_ref``. An empty forbidden field is
still rejected, so generic clients must omit fields that do not apply. These
references are identifiers such as ``form.contact.request``,
``website.contactus`` and ``crm.lead``. They must not contain routes with query
strings, form values, DOM text or personal data.

Unknown fields are rejected, so names, e-mail addresses, phone numbers and form
bodies cannot enter this ingress. URL query strings, fragments and credentials
are stripped by the canonical Marketing Touchpoint DTO.  The normalized landing
URL must have the exact same scheme, host and effective port as the request
Origin; listing two origins on one endpoint does not let one origin claim a
landing on the other.

``entry_point`` keeps the original ``marketing.web.ingress.v1`` source schema,
mapping version 1 and canonical digest shape. Action events use source schema
``marketing.web.ingress.v2`` and mapping version 2. Their normalized references
are projected into the same immutable ledger as ``web.action``, ``web.route``
and, for a form, ``web.model`` asset references. No parallel Website ledger is
created.  Every new event records one classified ingress provenance and projects
it to ``web_ingress.provenance`` in the touchpoint extension.  Historical rows
whose old schema cannot prove their path are migrated to ``unclassified``;
new rows cannot use that value.

Privacy and durability
----------------------

Optional capture is **blocked by default**, including after an upgrade of an
unconfigured endpoint. A Marketing Administrator must explicitly configure a
purpose code, policy and notice versions, documented non-consent basis and
justification, and a positive identifier retention duration before enabling it.
There is no default legal basis or retention duration. The actor/time and policy
version are recorded. Disabling capture increments the configuration fence and
blocks both browser POSTs and trusted Website action capture.

This release has no trusted CMP/individual consent producer. Its configuration
does not grant visitor consent, and consent-based optional capture must stay
disabled until that producer exists. Browser ``granted`` remains rejected;
ordinary Website events retain ``consent_state=unknown`` while carrying the
administrator's separate policy/basis snapshot. A documented non-consent policy
must be appropriate to the deployment; the software does not make that assessment.

Click identifiers are hashed in ``marketing.attribution.identifier``. Their raw
values are retained only in the ACL-protected
``marketing.web.ingress.click.value`` vault and connected to the ledger through
``value_ref``. Accepted-event diagnostics contain hashes and normalized metadata
only. HTTP responses never expose event, touchpoint or vault references, and the
controller never logs request bodies or submitted values.

Public browser and confirmed Website action paths cannot assert a consent
decision; their value must remain ``unknown``.  A non-unknown decision is
accepted only from the internal server seam and is labelled ``server_internal``
in the privacy snapshot.  This contract keeps a public capability from
manufacturing the legal basis used by downstream activation.

Each accepted event snapshots its retention deadline from observation time.
An hourly cleanup processes at most 100 expired events per run. It erases raw
click values, correlatable click/session/visitor hashes and identifier references,
and the touchpoint's free-form URL/UTM/extension values. Unrelated opaque
tombstones preserve row identity without preserving the original matching hash.
Canonical keys, evidence digests and event dedupe remain so retries and revised
replays cannot restore a retired occurrence. This is minimization, not a claim
that the remaining technical evidence is legally anonymous.

Pre-existing events without a deadline are visibly counted on the endpoint.
``Review legacy events and proposed deadlines`` is a read-only preview.
``Apply policy to next 100 legacy events`` is a separate explicit operator action:
it uses each original observation date and records actor, time and policy version.
The documented policy must be complete; optional capture can remain disabled
while assigning deadlines and erasing legacy values.
Expired legacy values become eligible for cleanup. No cron guesses a duration,
and existing deadlines are never extended by a policy edit or repeated action.
Rehearse assignment and erasure in a copy before production use.

Erasure only covers this ingress and its attribution copies. It does not erase
native CRM records, provider Lead Ads ledgers, backups or other integrations;
those require their own retention and subject-request policy. The native form
continues creating its record when optional marketing capture is blocked.

Security boundary
-----------------

The rotatable public key is deliberately **not a secret**: browser code and site
visitors can observe it. It identifies and revokes a source configuration; it is
not HMAC authentication and does not prove that a human performed the visit.
Exact Origin and landing-host allowlists reduce accidental/cross-site intake but
are also not proof against a custom HTTP client. Server-side conversion imports
must use provider credentials and their own verification flow.

Generate ``event_id``, ``session_ref`` and ``visitor_ref`` as high-entropy opaque
values (UUIDv4 or equivalent). The ledger stores their SHA-256 comparison hashes;
predictable identifiers can therefore be brute-forced and must not encode a user,
e-mail address or phone number.

``event_id`` is hashed before persistence. Endpoint/event admission is serialized
with a PostgreSQL transaction advisory lock and a unique constraint. A replay of
the same normalized event is idempotent; reuse of an event ID with different
content is first-wins and receives an opaque HTTP 409 response. Successful and
duplicate requests both receive the same reference-free HTTP 202 body. All event,
vault and touchpoint writes are one transaction.  Both ORM guards and a database
constraint require an event to move directly from ``processing`` without a
projection to ``done`` with its touchpoint; partial/fabricated terminal states
are rejected. Provenance is also first-wins:
an exact replay through a different adapter cannot upgrade or downgrade the
classification of the event that already owns the identifier.

Before ingestion, a payload-free application admission ledger applies the
endpoint ceiling independently to generic entry points, native Website forms
and Website WhatsApp handoffs.  It deliberately stores no IP address, URL or
request body.  The non-serializing count/insert design can overshoot by the
number of concurrent Odoo transactions, so it is a database safety net rather
than an edge DDoS control.  HTTP ``429`` includes ``Retry-After: 60``.
Admission rows are purged in bounded batches every minute after their short
operational window; they are neither attribution evidence nor analytics.

Operations
----------

Rotate the public key from the endpoint form to revoke an existing embed. Keep
the replay window short (five minutes by default). Multi-company record rules
scope configuration and evidence to active companies. Database administrators
can still read protected vault columns; application-level ACL is not database
encryption. A future hardening step can move vault values to an encrypted secret
backend without changing the ledger's ``value_ref`` contract.

Production ingress must also have reverse-proxy body and rate limits, and a
single unambiguous Odoo database selected by host/dbfilter. Neither CORS nor the
public key protects the endpoint from a custom HTTP client that replays observed
configuration.  The application ceiling does not replace a per-IP and global
Traefik limit; its purpose is to contain an accidental proxy/configuration gap
without persisting visitor identity.

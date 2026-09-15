Marketing Center CRM
====================

Links immutable marketing touchpoints to CRM leads and projects auditable CRM
lifecycle changes into the provider-neutral Marketing Center business-event ledger.

Native campaign classification
------------------------------

An optional asynchronous classifier fills the standard CRM campaign, source and
medium from resolved external campaigns. Configure each Marketing Center source
as Disabled (default), Simulation or Apply. Missing native campaigns can be
created automatically; manual CRM classifications remain protected. The
Acquisition tab provides preview, reconciliation, audit receipts and safe undo.

See the `configuration and flow guide <../docs/native-campaign-attribution.md>`_
for module responsibilities, diagrams, technical fields and upgrade guidance.

Touchpoint/lead links use an append-only authority ledger.  Every producer adds
its own immutable assertion with an idempotency reference; removing a producer's
source record appends an immutable revocation.  The effective SQL projection
keeps a link visible while at least one independent authority still asserts it.

CRM lifecycle contract
----------------------

Marketing evidence never owns the lifecycle of a ``crm.lead``. Each immutable
link stores the original model, database ID, company and a bounded display
reference; its optional live ``lead_id`` uses ``ondelete="set null"``. Deleting a
lead therefore removes navigation, not evidence. Native CRM merges append an
explicit source-to-survivor equivalence and new survivor projections while the
original assertions and event links remain unchanged. Cross-company evidence is
never transferred by a merge.

Consumer service contract
-------------------------

Optional integrations must use ``marketing.crm.service`` rather than create
ledger rows directly.  A producer supplies a stable namespace and source-record
identity, plus an idempotency key for the concrete assertion::

    service._link_touchpoint_lead(
        touchpoint,
        lead,
        source_ref="provider evidence reference",
        authority_key="producer.module",
        authority_ref="stable source record reference",
        assertion_ref="stable source/canonical-touchpoint/lead assertion",
    )

When that source relation disappears, the same authority appends revocations;
it cannot revoke assertions owned by another producer::

    service._revoke_authority_assertions(
        lead,
        "producer.module",
        "stable source record reference",
        "stable source unlink occurrence",
        reason="source_relation_removed",
    )

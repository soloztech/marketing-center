=============
Meta API Base
=============

Technical, domain-neutral Meta integration primitives for Odoo 16.

The initial release provides a bounded Meta Graph client, exact-byte webhook
signature verification, Graph version validation and a stable technical error
taxonomy. It contains no conversation, campaign, lead or marketing domain model.

The Graph functions accept a technical App object exposing ``ensure_one()``,
``external_app_id``, ``graph_version``, ``app_secret`` and ``active``. Credentials
exist only in memory for the duration of a request; this addon defines no model or
field in which they could be persisted.

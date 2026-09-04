=============
Meta API Base
=============

Technical, domain-neutral Meta integration primitives for Odoo 16.

The addon provides a bounded Meta Graph client, exact-byte webhook signature
verification, Graph version validation, a stable technical error taxonomy and a
shared ``meta.api.app`` configuration boundary. It contains no conversation,
campaign, lead or marketing domain model.

``meta.api.app`` persists company, App identity, pinned Graph version and an opaque
environment/file reference. It never persists an App Secret. ``_resolve_runtime()``
locks and fences the configuration revision, resolves the referenced secret and
returns an immutable ``MetaRuntimeApp`` whose secret exists only in process memory.
The runtime explicitly rejects pickle/queue serialization.

The Graph functions accept either that runtime object or a compatible technical App
object exposing ``ensure_one()``, ``external_app_id``, ``graph_version``,
``app_secret`` and ``active``. Request paths, primitive-tree complexity and aggregate
request/response bytes are bounded. Endpoint/Page/user tokens remain consumer-owned
and can resolve through the shared ``resolve_secret()`` helper without entering this
model. Rate limits are a retryable error class; authentication/configuration failures
remain paused, and ambiguous mutating outcomes remain explicitly uncertain.

Only Odoo Settings administrators (``base.group_system``) have model ACLs. There are
no menus or views in this technical addon.

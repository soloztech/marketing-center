Technical, domain-neutral Meta integration primitives for Odoo 16. The addon
provides a bounded Meta Graph client, exact-byte webhook signature verification,
Graph version validation, a stable technical error taxonomy and a shared
``meta.api.app`` configuration boundary. It contains no conversation, campaign,
lead or marketing-domain model and has no dependency on Contact Center, Marketing
Center, CRM, Website or queue workers.

``meta.api.app`` persists company, App identity, pinned Graph version and an opaque
environment/file reference. It never persists an App Secret. ``_resolve_runtime()``
locks and fences the configuration revision, resolves the referenced secret and
returns an immutable ``MetaRuntimeApp`` whose secret exists only in process memory.
The runtime explicitly rejects pickle and queue serialization.

Graph functions accept either that runtime or a compatible process-memory technical
App object exposing ``ensure_one()``, ``active``, ``external_app_id``,
``graph_version`` and ``app_secret``. Request paths, primitive-tree complexity and
aggregate request/response bytes are bounded. Endpoint, Page and user tokens remain
consumer-owned and can resolve through the shared ``resolve_secret()`` helper
without entering this model. Rate limits are retryable; authentication and
configuration failures pause processing; ambiguous mutating outcomes remain
explicitly uncertain.

Only Odoo Settings administrators (``base.group_system``) have model ACLs, and the
record rule limits Apps to active companies. There are no menus or views in this
technical addon.

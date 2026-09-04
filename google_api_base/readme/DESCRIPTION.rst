Technical, domain-neutral Google Ads API primitives for Odoo 16.

The addon stores a company-scoped, revision-fenced ``google.api.identity``. It
supports both an authorized-user refresh token and the official Google Ads service
account workflow. OAuth fields, the service-account JSON and the developer token are
always opaque environment/file references. Credential values and short-lived access
tokens exist only in immutable process-memory contracts and are excluded from their
representations. Service-account JWT signing and exchange use Google's maintained
``google-auth`` library; no private-key implementation lives in this addon.

``GoogleAdsFacade`` exposes only read-only customer discovery and bounded Google
Ads Search pagination over the fixed REST v25 endpoints. URLs are fixed, redirects
are disabled, responses are streamed under byte limits and quota, authentication,
permission and transient failures use a sanitized error taxonomy. No campaign, lead,
attribution or other business-domain model lives here.

Only Odoo Settings administrators (``base.group_system``) receive model ACLs, and
the record rule limits identities to active companies. This technical addon has no
menus or views; consumer addons own their configuration experience.

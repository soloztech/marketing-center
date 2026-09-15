Provider and bridge addons create touchpoints through
``marketing.attribution.service._ingest_touchpoint``.  The leading underscore keeps
the Python service outside the public RPC surface. Ledger models reject direct
create, write and unlink operations, including from administrators.

Native campaign classification
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

In a marketing source's **Native UTM** tab, select a native source and medium,
then choose **Simulation** or **Apply**. The default is **Disabled**. Simulation
resolves existing associations and reports missing campaigns without creating
campaigns or associations. Downstream integrations may record their own simulation
reports; the base resolution service itself performs no business writes.

In Apply mode, ``Create Missing Native Campaigns`` permits automatic creation of
``utm.campaign`` for a resolved external campaign. Its human-readable title comes
from the external campaign. Its unique native identifier contains the catalog
entity UUID. Campaign names are never used to discover or merge associations.
Renaming the external campaign preserves the native record and its title.

Administrators can instead select an existing native campaign on the external
campaign's **Native UTM** tab. Several external campaigns may share the same
native campaign. The origin, time and user of the current association are stored.
Clearing an association also enables **Block Native Classification**; explicitly
unblock it to permit automatic replacement. Linked native campaigns cannot be
deleted while their association exists. Removed, archived or missing external
campaigns retain their association but are not applied to new classifications.

``marketing.native.utm.service._resolve_touchpoint(touchpoint, apply=False)`` reads
only the accepted effective revision and its catalog resolutions. Raw URL text,
UTM labels and campaign names are not evidence of an external identity. Campaigns
can be resolved directly or through an ad's catalog parent chain. Ambiguous or
conflicting campaign identities are reported without choosing one. Company scope
is checked before automatic creation, and row locks force stale concurrent
requests to retry instead of creating duplicate campaigns.

The result contains ``state``, ``reason``, ``source_id`` (the Marketing Center
source), ``entity_id``, ``campaign_id`` (native), ``utm_source_id``, ``medium_id``,
``source_mode`` and ``created``. States are ``ready``, ``would_create``, ``missing``,
``conflict`` or ``disabled``. Consumers must honor ``source_mode`` before writing.
The optional CRM bridge owns CRM classification, writer provenance and preservation
of manual CRM values. This base module does not write CRM fields.

Configuration, mapping and asset-resolution changes invoke the private extension
hook ``_after_native_utm_change(source_ids=None, entity_ids=None,
touchpoint_ids=None)``. Downstream modules should schedule work rather than write
business records inside this callback. The complete architecture is documented in
`CRM intake and attribution <../docs/crm-intake-and-attribution.md>`_.

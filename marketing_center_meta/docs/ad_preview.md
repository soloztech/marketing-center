# Optional Contact Center ad preview enrichment

The Contact Center background preview job can supplement missing referral copy
and images when the inbox explicitly enables ad preview enrichment. The bridge
module remains optional and does not depend on the Meta module. Without the Meta
preview service it returns no additional fields.

The bridge requires the exact current Contact Center attribution link, its
current effective Marketing touchpoint, and one unambiguous catalog ad/source.
An identifier named `meta.source_id` is accepted only when the existing catalog
resolution identifies an ad. Campaigns, stale links, unresolved identifiers,
cross-company records and ambiguous sources do not trigger a remote read.

The Meta service requires one ready reader connection to that source, an active
and healthy `ads_reader` profile, verified `ads_read`, and `read_entities` on
both the source and connection. Expired credentials, cooldowns and revision
mismatches stop the lookup. Configuration and credential references use the
existing Meta app/profile configuration; this feature introduces no credentials.

One Graph v26.0 GET reads the ad and its expanded creative. Both returned account
identities must match the authorized catalog source. Source, connection, profile,
app and entity revisions are checked again after the response. Authorization
locks are acquired only after HTTP, allowing a concurrent revocation to reject
the result under the database transaction isolation contract.

The result contains only available `title` (256 characters), `body` (2000),
`public_url`, `thumbnail_url` and `media_type`. Ad and creative management names
are not customer-facing copy. A real Instagram permalink is preferred; otherwise
the reported creative destination can be used. Public URLs have their query and
fragment removed. The Contact Center currently displays only authorized public
Meta permalinks, so an external creative destination may be omitted by its final
presentation filter. Signed HTTPS thumbnail locators remain internal and must pass
the Contact Center downloader/vault validation before a private thumbnail can be
served to an operator. They must never be included in a browser DTO.

The existing Graph helper applies timeouts, disables redirects and bounds the
response to 128 KiB. Optional lookup failures return no enrichment without
logging provider exceptions, URLs or tokens. The service neither posts content
nor changes catalog entities or credential health.

Field definitions follow the official Meta Business SDK:

- [Ad](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/ad.py)
- [AdCreative](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/adcreative.py)
- [AdCreativeLinkData](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/adcreativelinkdata.py)

The ORM regression suites in `marketing_center_meta/tests/test_ad_preview.py`
and `marketing_center_contact_center/tests/test_ad_preview.py` use only synthetic
credentials and mocked HTTP. Run them with `--test-tags marketing_ad_preview`
alongside the updated Contact Center module.

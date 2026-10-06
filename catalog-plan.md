# Content library moved

The content catalog introduced here is now
[Content Center](https://github.com/soloztech/content-center/tree/16.0). Its
[original plan](https://github.com/soloztech/content-center/blob/16.0/catalog-plan.md)
and
[migration guide](https://github.com/soloztech/content-center/blob/16.0/MIGRATION.md)
remain available with source provenance.

Use `content_center_base`, optionally `content_center_sale` and
`content_center_contact_center`. The extraction preserves existing records and IDs
through a validated ORM module rename; do not uninstall the legacy addons. The
advertising catalog service remains in Marketing Center.

Install ``marketing_center_base`` on Odoo 16. Direct dependencies are ``base``,
``utm``, ``crm`` and OCA ``queue_job``. Install only the optional integrations
required by your operation. A fresh installation needs no legacy CRM/dashboard
package. CRM availability does not automatically create a lead on every message.

For an existing installation, upgrade the core together with installed legacy
packages and affected bridges. Do not upgrade a legacy package alone or uninstall
it as a cleanup step. See ``docs/core-fusion.md`` for the migration procedure.

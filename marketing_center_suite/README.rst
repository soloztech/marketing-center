Marketing Center
================

This addon is the supported Soloz installation facade for the complete Marketing
Center on Odoo 16. It contains no business model, controller, scheduled action,
security rule or menu of its own. Installing it selects the tested set of provider,
first-party acquisition, CRM, Contact Center, Sales, Accounting and dashboard
addons while those components remain independently installable.

The user interface continues to have one canonical root menu, owned by
``marketing_center_base``. This facade is the single application shown in the Odoo
Apps catalog; the component addons are technical modules.

Included capabilities
---------------------

* provider-neutral ledgers, synchronization and attribution;
* read-only Google Ads and Meta Ads integrations;
* Meta Lead Ads and native Odoo Website form correlation with CRM;
* Contact Center, CRM, Sales and Accounting lifecycle projections;
* the operational Marketing Center dashboard.

Installation profiles
---------------------

The recommended Soloz deployment installs ``marketing_center_suite``. Advanced
deployments that intentionally need a smaller dependency graph may install these
entry addons directly:

* core analytics: ``marketing_center_dashboard``;
* paid media: ``marketing_center_google`` and ``marketing_center_meta``;
* acquisition and CRM: ``marketing_center_meta_crm`` and
  ``marketing_center_website_crm``;
* service and CRM: ``marketing_center_contact_center_crm``;
* commercial revenue: ``marketing_center_sale_account``.

This module is an installation and compatibility marker, not a lifecycle owner.
Uninstalling only ``marketing_center_suite`` does not uninstall its dependencies or
remove their data. Components must be removed explicitly and only after evaluating
their individual retention and dependency contracts.

Adding another component to the supported full installation is a reviewed suite
contract change and requires a suite version bump, clean-install test and upgrade
replay.

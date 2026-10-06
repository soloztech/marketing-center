from odoo import SUPERUSER_ID, api


def uninstall_hook(cr, registry):
    from odoo.addons.marketing_center_base.integration_migration import (
        remove_owner_aliases,
    )

    remove_owner_aliases(
        api.Environment(cr, SUPERUSER_ID, {}), "marketing_center_website"
    )

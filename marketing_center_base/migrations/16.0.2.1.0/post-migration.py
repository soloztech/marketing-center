from odoo import SUPERUSER_ID, api

# Native migrations are loaded outside the addon package.
# pylint: disable=odoo-addons-relative-import
from odoo.addons.marketing_center_base.integration_migration import checkpoint


def migrate(cr, version):
    checkpoint(api.Environment(cr, SUPERUSER_ID, {}), "marketing_center_base")

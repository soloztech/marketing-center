from odoo import SUPERUSER_ID, api

from odoo.addons.marketing_center_base.integration_migration import checkpoint


def migrate(cr, version):
    checkpoint(api.Environment(cr, SUPERUSER_ID, {}), "marketing_center_website")

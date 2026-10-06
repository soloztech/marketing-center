# pylint: disable=odoo-addons-relative-import
# Native migration scripts are loaded by file path, without the addon package.
from odoo import SUPERUSER_ID, api

from odoo.addons.marketing_center_website.ingress_migration import checkpoint


def migrate(cr, version):
    checkpoint(api.Environment(cr, SUPERUSER_ID, {}), "marketing_center_website")

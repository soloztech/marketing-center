# pylint: disable=odoo-addons-relative-import
# Native migration scripts are loaded by file path, without the addon package.
from odoo import SUPERUSER_ID, api

from odoo.addons.marketing_center_website.ingress_migration import assert_prepared


def migrate(cr, version):
    assert_prepared(api.Environment(cr, SUPERUSER_ID, {}))

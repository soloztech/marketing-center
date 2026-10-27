from odoo import SUPERUSER_ID, api

from odoo.addons.marketing_center_base.integration_migration import assert_prepared


def migrate(cr, version):
    assert_prepared(api.Environment(cr, SUPERUSER_ID, {}))

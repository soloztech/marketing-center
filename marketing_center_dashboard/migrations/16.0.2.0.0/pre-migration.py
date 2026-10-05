from odoo import SUPERUSER_ID, api

from odoo.addons.marketing_center_base.core_migration import require_fused_core


def migrate(cr, version):
    require_fused_core(api.Environment(cr, SUPERUSER_ID, {}))

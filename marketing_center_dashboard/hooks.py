from odoo import SUPERUSER_ID, api

from odoo.addons.marketing_center_base.core_migration import (
    ensure_legacy_aliases,
    require_fused_core,
)


def pre_init_hook(cr):
    require_fused_core(api.Environment(cr, SUPERUSER_ID, {}))


def post_init_hook(cr, registry):
    env = api.Environment(cr, SUPERUSER_ID, {})
    require_fused_core(env)
    ensure_legacy_aliases(env, modules=("marketing_center_dashboard",))

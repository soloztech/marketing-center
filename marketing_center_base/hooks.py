from odoo import SUPERUSER_ID, api

from .core_migration import ensure_legacy_aliases, remove_legacy_aliases


def post_init_hook(cr, registry):
    ensure_legacy_aliases(api.Environment(cr, SUPERUSER_ID, {}))


def uninstall_hook(cr, registry):
    remove_legacy_aliases(api.Environment(cr, SUPERUSER_ID, {}))

# Migration scripts are loaded outside the addon package by Odoo.
# pylint: disable=odoo-addons-relative-import
from odoo import SUPERUSER_ID, api

from odoo.addons.marketing_center_base.core_migration import (
    ensure_legacy_aliases,
    initialize_legacy_crm_capture_stamp,
)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    initialize_legacy_crm_capture_stamp(env)
    ensure_legacy_aliases(env)

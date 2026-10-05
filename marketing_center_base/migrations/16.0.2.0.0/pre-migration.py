# Migration scripts are loaded outside the addon package by Odoo.
# pylint: disable=odoo-addons-relative-import
from odoo import SUPERUSER_ID, api

from odoo.addons.marketing_center_base.core_migration import adopt_legacy_records


def migrate(cr, version):
    adopt_legacy_records(api.Environment(cr, SUPERUSER_ID, {}))

"""Keep native Google script guards safe when the editor restores a view."""
from odoo import SUPERUSER_ID, _, api
from odoo.exceptions import UserError


def normalize_measurement(env):
    canonical = env.ref("marketing_center_website.measurement_layout")
    views = (
        env["ir.ui.view"]
        .with_context(active_test=False, lang=None, no_cow=True, no_save_prev=True)
        .search([("key", "=", canonical.key)])
    )
    for view in views:
        arches = view._fields["arch_db"]._get_stored_translations(view) or {}
        if not arches or any(
            "_marketing_measurement_managed" in arch
            or arch.count("_marketing_native_google_allowed") != 2
            for arch in arches.values()
        ):
            raise UserError(_("Native Google layout has not been loaded"))
        view.write({"arch_prev": view.arch_db})


def migrate(cr, version):
    normalize_measurement(api.Environment(cr, SUPERUSER_ID, {}))

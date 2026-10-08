"""Do not let Reset restore the retired notice configuration attributes."""
from odoo import SUPERUSER_ID, _, api
from odoo.exceptions import UserError


def normalize_measurement(env):
    view = env.ref("marketing_center_website.measurement_layout")
    view = view.with_context(lang="en_US", no_cow=True, no_save_prev=True)
    if any(
        token in view.arch_db
        for token in (
            "data-cookie-notice",
            "data-cookie-proceed-label",
            "data-cookie-policy-url",
        )
    ):
        raise UserError(
            _("New measurement layout not loaded; previous attributes cannot retire")
        )
    view.write({"arch_prev": view.arch_db})


def migrate(cr, version):
    normalize_measurement(api.Environment(cr, SUPERUSER_ID, {}))

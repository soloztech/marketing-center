from . import models

from .hooks import post_init_hook, uninstall_hook

from odoo.addons.marketing_center_base.legacy_python import register_aliases

register_aliases(
    "marketing_center_meta_crm",
    "odoo.addons.marketing_center_meta.models.crm",
)

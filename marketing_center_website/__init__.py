from . import services, models, controllers
from .hooks import pre_init_hook, uninstall_hook
from .ingress_aliases import register_ingress_aliases

from odoo.addons.marketing_center_base.legacy_python import register_aliases

register_aliases(
    "marketing_center_website_crm",
    "odoo.addons.marketing_center_website.models.crm",
    "odoo.addons.marketing_center_website.controllers",
)


register_ingress_aliases()

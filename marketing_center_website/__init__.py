from . import controllers, models, services
from .hooks import uninstall_hook

from odoo.addons.marketing_center_base.legacy_python import register_aliases

register_aliases(
    "marketing_center_website_crm",
    "odoo.addons.marketing_center_website.models.crm",
    "odoo.addons.marketing_center_website.controllers",
)

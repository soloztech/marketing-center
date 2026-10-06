from . import models
from .hooks import post_init_hook, uninstall_hook

from .legacy_python import register_aliases, register_dashboard_alias

register_aliases("marketing_center_crm", "odoo.addons.marketing_center_base.models.crm")
register_dashboard_alias()

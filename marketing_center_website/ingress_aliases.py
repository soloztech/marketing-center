"""Historical ingress imports share the owner's exact loaded modules."""
import sys

from odoo.addons.marketing_center_base.legacy_python import register_aliases


def register_ingress_aliases():
    legacy = "odoo.addons.marketing_center_web_ingress"
    register_aliases(
        "marketing_center_web_ingress",
        "odoo.addons.marketing_center_website.models.ingress",
        "odoo.addons.marketing_center_website.controllers.ingress",
    )
    original = "odoo.addons.marketing_center_website.services.ingress"
    for name, module in list(sys.modules.items()):
        if name == original or name.startswith(original + "."):
            target = legacy + ".services" + name[len(original) :]
            sys.modules[target] = module
            parent, _, leaf = target.rpartition(".")
            if parent in sys.modules:
                setattr(sys.modules[parent], leaf, module)

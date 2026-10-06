"""Old import paths share loaded implementations without installing old addons."""

import importlib.machinery
import sys
from types import ModuleType


def _package(name):
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    package = ModuleType(name)
    package.__path__ = []
    package.__package__ = name
    package.__spec__ = importlib.machinery.ModuleSpec(
        name, loader=None, is_package=True
    )
    sys.modules[name] = package
    parent, _, leaf = name.rpartition(".")
    setattr(sys.modules[parent], leaf, package)
    return package


def register_aliases(legacy, model_package, controller_package=None):
    """Register only modules already imported by their functional owner."""
    root = "odoo.addons." + legacy
    _package(root)
    for original, alias in (
        (model_package, root + ".models"),
        (controller_package, root + ".controllers"),
    ):
        if not original:
            continue
        for name, module in list(sys.modules.items()):
            if name == original or name.startswith(original + "."):
                target = alias + name[len(original) :]
                sys.modules[target] = module
                parent, _, leaf = target.rpartition(".")
                if parent in sys.modules:
                    setattr(sys.modules[parent], leaf, module)


def register_dashboard_alias():
    root = "odoo.addons.marketing_center_dashboard"
    _package(root)
    models = _package(root + ".models")
    dashboard = sys.modules["odoo.addons.marketing_center_base.models.dashboard"]
    models.dashboard = dashboard
    sys.modules[root + ".models.dashboard"] = dashboard

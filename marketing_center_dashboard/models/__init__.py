"""Legacy Python paths alias the core modules, including opaque tokens."""

import importlib
import sys

for _name in ("dashboard",):
    _module = importlib.import_module(
        "odoo.addons.marketing_center_base.models." + _name
    )
    globals()[_name] = _module
    sys.modules[__name__ + "." + _name] = _module

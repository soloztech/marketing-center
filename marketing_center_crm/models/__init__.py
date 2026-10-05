"""Legacy Python paths alias the core modules, including opaque tokens."""

import importlib
import sys

for _name in (
    "campaign_board",
    "crm_lead",
    "crm_stage",
    "event_policy",
    "links",
    "native_utm",
    "service",
    "tokens",
):
    _module = importlib.import_module(
        "odoo.addons.marketing_center_base.models.crm." + _name
    )
    globals()[_name] = _module
    sys.modules[__name__ + "." + _name] = _module

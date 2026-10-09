"""Current namespace imports exercised with both complete addon registrations."""
import importlib
import sys

from odoo.tests.common import SavepointCase


class TestMarketingCenterWebsiteCurrentImports(SavepointCase):
    def test_current_addon_namespaces_and_retired_registration(self):
        addon = importlib.import_module("odoo.addons.marketing_center_website")
        self.assertTrue(addon.models)
        for package, leaves in {
            "models.crm": (
                "correlation",
                "ingress_event",
                "intent",
                "ip_observation",
                "native_submission",
                "service",
                "tokens",
            ),
            "models.ingress": ("admission", "endpoint", "event", "service"),
            "services.ingress": ("contracts", "errors", "tokens"),
            "controllers.ingress": ("ingress",),
        }.items():
            for leaf in leaves:
                name = "odoo.addons.marketing_center_website." + package + "." + leaf
                self.assertEqual(importlib.import_module(name).__name__, name)
        for retired in (
            "odoo.addons.marketing_center_web_ingress",
            "odoo.addons.marketing_center_website_crm",
        ):
            self.assertFalse(
                any(
                    name == retired or name.startswith(retired + ".")
                    for name in sys.modules
                )
            )

    def test_native_helpers_remain_current_namespace_exports(self):
        original = importlib.import_module(
            "odoo.addons.marketing_center_website.models.crm.native_submission"
        )
        shared = importlib.import_module(
            "odoo.addons.marketing_center_website.services.acquisition"
        )
        for name in ("safe_page", "acquisition_values", "utc_iso"):
            self.assertIs(getattr(original, name), getattr(shared, name))

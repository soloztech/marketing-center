import importlib

from odoo.tests.common import SavepointCase


class TestMarketingCenterWebsiteLegacyImports(SavepointCase):
    def test_legacy_model_paths_share_loaded_implementation(self):
        self.assertIs(
            importlib.import_module(
                "odoo.addons.marketing_center_website_crm.models.correlation"
            ),
            importlib.import_module(
                "odoo.addons.marketing_center_website.models.crm.correlation"
            ),
        )
        self.assertIs(
            importlib.import_module(
                "odoo.addons.marketing_center_website_crm.models.ingress_event"
            ),
            importlib.import_module(
                "odoo.addons.marketing_center_website.models.crm.ingress_event"
            ),
        )
        self.assertIs(
            importlib.import_module(
                "odoo.addons.marketing_center_website_crm.models.intent"
            ),
            importlib.import_module(
                "odoo.addons.marketing_center_website.models.crm.intent"
            ),
        )
        self.assertIs(
            importlib.import_module(
                "odoo.addons.marketing_center_website_crm.models.ip_observation"
            ),
            importlib.import_module(
                "odoo.addons.marketing_center_website.models.crm.ip_observation"
            ),
        )
        self.assertIs(
            importlib.import_module(
                "odoo.addons.marketing_center_website_crm.models.native_submission"
            ),
            importlib.import_module(
                "odoo.addons.marketing_center_website.models.crm.native_submission"
            ),
        )
        self.assertIs(
            importlib.import_module(
                "odoo.addons.marketing_center_website_crm.models.service"
            ),
            importlib.import_module(
                "odoo.addons.marketing_center_website.models.crm.service"
            ),
        )
        self.assertIs(
            importlib.import_module(
                "odoo.addons.marketing_center_website_crm.models.tokens"
            ),
            importlib.import_module(
                "odoo.addons.marketing_center_website.models.crm.tokens"
            ),
        )

    def test_legacy_ingress_packages_and_modules_share_loaded_implementation(self):
        for package, leaves in {
            "models": ("admission", "endpoint", "event", "service"),
            "services": ("contracts", "errors", "tokens"),
            "controllers": ("ingress",),
        }.items():
            current = "odoo.addons.marketing_center_website." + package + ".ingress"
            legacy = "odoo.addons.marketing_center_web_ingress." + package
            for suffix in ("",) + tuple("." + leaf for leaf in leaves):
                self.assertIs(
                    importlib.import_module(legacy + suffix),
                    importlib.import_module(current + suffix),
                )
        old_tokens = importlib.import_module(
            "odoo.addons.marketing_center_web_ingress.services.tokens"
        )
        new_tokens = importlib.import_module(
            "odoo.addons.marketing_center_website.services.ingress.tokens"
        )
        self.assertIs(
            old_tokens.WEB_INGRESS_INTERNAL_TOKEN, new_tokens.WEB_INGRESS_INTERNAL_TOKEN
        )
        old_errors = importlib.import_module(
            "odoo.addons.marketing_center_web_ingress.services.errors"
        )
        new_errors = importlib.import_module(
            "odoo.addons.marketing_center_website.services.ingress.errors"
        )
        self.assertIs(
            old_errors.WebIngressSerializationFailure,
            new_errors.WebIngressSerializationFailure,
        )
        contracts = importlib.import_module(
            "odoo.addons.marketing_center_website.services.ingress.contracts"
        )
        self.assertEqual(
            contracts.parse_web_ingress_payload.__module__, contracts.__name__
        )

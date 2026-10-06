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

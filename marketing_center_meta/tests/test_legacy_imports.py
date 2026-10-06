import importlib

from odoo.tests.common import SavepointCase


class TestMarketingCenterMetaLegacyImports(SavepointCase):
    def test_legacy_model_paths_share_loaded_implementation(self):
        self.assertIs(
            importlib.import_module(
                "odoo.addons.marketing_center_meta_crm.models.company"
            ),
            importlib.import_module(
                "odoo.addons.marketing_center_meta.models.crm.company"
            ),
        )
        self.assertIs(
            importlib.import_module(
                "odoo.addons.marketing_center_meta_crm.models.projection"
            ),
            importlib.import_module(
                "odoo.addons.marketing_center_meta.models.crm.projection"
            ),
        )
        self.assertIs(
            importlib.import_module(
                "odoo.addons.marketing_center_meta_crm.models.route"
            ),
            importlib.import_module(
                "odoo.addons.marketing_center_meta.models.crm.route"
            ),
        )
        self.assertIs(
            importlib.import_module(
                "odoo.addons.marketing_center_meta_crm.models.service"
            ),
            importlib.import_module(
                "odoo.addons.marketing_center_meta.models.crm.service"
            ),
        )
        self.assertIs(
            importlib.import_module(
                "odoo.addons.marketing_center_meta_crm.models.tokens"
            ),
            importlib.import_module(
                "odoo.addons.marketing_center_meta.models.crm.tokens"
            ),
        )

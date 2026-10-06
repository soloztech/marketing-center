import importlib

from odoo.tests.common import SavepointCase


class TestMarketingCenterContactCenterLegacyImports(SavepointCase):
    def test_legacy_model_paths_share_loaded_implementation(self):
        self.assertIs(
            importlib.import_module(
                "odoo.addons.marketing_center_contact_center_crm.models.company"
            ),
            importlib.import_module(
                "odoo.addons.marketing_center_contact_center.models.crm.company"
            ),
        )
        self.assertIs(
            importlib.import_module(
                "odoo.addons.marketing_center_contact_center_crm.models.crm_lead"
            ),
            importlib.import_module(
                "odoo.addons.marketing_center_contact_center.models.crm.crm_lead"
            ),
        )
        self.assertIs(
            importlib.import_module(
                "odoo.addons.marketing_center_contact_center_crm.models.hooks"
            ),
            importlib.import_module(
                "odoo.addons.marketing_center_contact_center.models.crm.hooks"
            ),
        )
        self.assertIs(
            importlib.import_module(
                "odoo.addons.marketing_center_contact_center_crm.models.service"
            ),
            importlib.import_module(
                "odoo.addons.marketing_center_contact_center.models.crm.service"
            ),
        )

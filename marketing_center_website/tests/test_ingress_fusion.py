from odoo.exceptions import ValidationError
from odoo.tests.common import SavepointCase

from ..hooks import pre_init_hook
from ..ingress_migration import MARKER_PREFIX, inverse


class TestWebsiteIngressFusionGuards(SavepointCase):
    def test_install_refuses_standalone_ingress(self):
        legacy = self.env["ir.module.module"].search(
            [("name", "=", "marketing_center_web_ingress")]
        )
        if not legacy:
            legacy = self.env["ir.module.module"].create(
                {"name": "marketing_center_web_ingress"}
            )
        legacy.state = "installed"
        with self.assertRaisesRegex(ValidationError, "predecessor Website"):
            pre_init_hook(self.env.cr)

    def test_committed_marker_blocks_inverse_before_receipt_access(self):
        self.env["ir.config_parameter"].sudo().set_param(
            MARKER_PREFIX + "marketing_center_website", "16.0.2.1.0"
        )
        with self.assertRaisesRegex(ValidationError, "committed fusion data"):
            inverse(self.env, {"format": 1})

    def test_pending_upgrade_blocks_inverse_without_a_marker(self):
        self.env["ir.config_parameter"].sudo().search(
            [("key", "=", MARKER_PREFIX + "marketing_center_website")]
        ).unlink()
        self.env["ir.module.module"].search(
            [("name", "=", "marketing_center_website")]
        ).state = "to upgrade"
        with self.assertRaisesRegex(ValidationError, "pending module flags"):
            inverse(self.env, {"format": 1})

from types import SimpleNamespace
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.modules.module import get_module_path
from odoo.tests import tagged
from odoo.tests.common import SavepointCase

from .. import integration_migration as fusion
from ..hooks import post_init_hook
from ..integration_migration import (
    MARKER_PREFIX,
    OWNERS,
    PREDECESSOR_VERSIONS,
    VERSIONS,
    assert_prepared,
    inverse,
    markers,
)


@tagged("-at_install", "post_install")
class TestMarketingIntegrationFusionGuards(SavepointCase):
    def test_legacy_import_aliases_are_not_addon_providers(self):
        for name in OWNERS:
            self.assertFalse(get_module_path(name, display_warning=False))

    def test_existing_owner_without_preparation_is_rejected(self):
        core = self.env["ir.module.module"].search(
            [("name", "=", "marketing_center_base")]
        )
        legacy = self.env["ir.module.module"].search([("name", "in", list(OWNERS))])
        if legacy:
            legacy[0].state = "installed"
            message = "prepare legacy ownership before upgrading"
        else:
            core.latest_version = PREDECESSOR_VERSIONS[core.name]
            message = "without bridges is unsupported"
        with self.assertRaisesRegex(ValidationError, message):
            assert_prepared(self.env)

    def test_orphan_legacy_metadata_is_rejected(self):
        record = self.env.ref("marketing_center_base.action_marketing_leads")
        self.env["ir.model.data"].create(
            {
                "module": "marketing_center_crm",
                "name": "unexpected_orphan",
                "model": record._name,
                "res_id": record.id,
            }
        )
        legacy = self.env["ir.module.module"].search([("name", "in", list(OWNERS))])
        message = (
            "legacy XML-id catalog differs" if legacy else "orphan legacy metadata"
        )
        with self.assertRaisesRegex(ValidationError, message):
            assert_prepared(self.env)

    def test_compact_whatsapp_version_reaches_fresh_metadata_guard(self):
        record = self.env.ref("marketing_center_base.action_marketing_leads")
        self.env["ir.model.data"].create(
            {
                "module": "marketing_center_crm",
                "name": "compact_version_orphan",
                "model": record._name,
                "res_id": record.id,
            }
        )
        modules = {
            name: SimpleNamespace(latest_version=version)
            for name, version in VERSIONS.items()
        }
        modules["marketing_center_website_whatsapp"].latest_version = "16.0.1.4.0"
        with patch.object(fusion, "_modules", return_value=modules):
            with self.assertRaisesRegex(ValidationError, "orphan legacy metadata"):
                assert_prepared(self.env)

    def test_compact_whatsapp_version_reaches_migrated_catalog_guard(self):
        modules = {
            name: SimpleNamespace(latest_version=version)
            for name, version in VERSIONS.items()
        }
        modules["marketing_center_website_whatsapp"].latest_version = "16.0.1.4.0"
        modules.update(
            {
                name: SimpleNamespace(state="uninstallable", latest_version=version)
                for name, version in fusion.LEGACY_VERSIONS.items()
            }
        )
        with patch.object(fusion, "_modules", return_value=modules), patch.object(
            fusion,
            "_rows",
            side_effect=ValidationError("reached canonical catalog guard"),
        ):
            with self.assertRaisesRegex(
                ValidationError, "reached canonical catalog guard"
            ):
                assert_prepared(self.env)

    def test_fresh_install_does_not_claim_upgrade_checkpoints(self):
        # Isolate the fresh-hook contract even when run on a migrated QA database.
        self.env["ir.config_parameter"].sudo().search(
            [("key", "in", [MARKER_PREFIX + name for name in VERSIONS])]
        ).unlink()
        post_init_hook(self.env.cr, self.env.registry)
        self.assertFalse(any(markers(self.env).values()))

    def test_pending_flags_refuse_inverse_before_any_metadata_write(self):
        # Exercise the no-checkpoint pending-flag guard on either database topology.
        self.env["ir.config_parameter"].sudo().search(
            [("key", "in", [MARKER_PREFIX + name for name in VERSIONS])]
        ).unlink()
        core = self.env["ir.module.module"].search(
            [("name", "=", "marketing_center_base")]
        )
        core.state = "to upgrade"
        with self.assertRaisesRegex(ValidationError, "pending module flags"):
            inverse(self.env, {"format": 1})
        self.assertEqual(core.state, "to upgrade")

    def test_committed_marker_refuses_inverse(self):
        name = "marketing_center_base"
        self.env["ir.config_parameter"].sudo().set_param(
            MARKER_PREFIX + name, VERSIONS[name]
        )
        with self.assertRaisesRegex(ValidationError, "committed fusion data"):
            inverse(self.env, {"format": 1})

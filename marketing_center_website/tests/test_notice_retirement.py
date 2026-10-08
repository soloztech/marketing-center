"""Fail closed on unknown CMS dependencies before retiring notice code."""
import runpy
from pathlib import Path

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestNoticeRetirement(TransactionCase):
    def setUp(self):
        super().setUp()
        path = (
            Path(__file__).resolve().parents[1]
            / "migrations/16.0.2.2.0/pre-migration.py"
        )
        self.retire = runpy.run_path(str(path))["retire_notice"]
        self.views = self.env["ir.ui.view"]

    def test_retired_reentry_is_noop(self):
        self.assertEqual(self.retire(self.env), {"removed_views": []})

    def test_website_copy_is_refused(self):
        self.views.create(
            {
                "name": "Unreviewed Website cookie copy",
                "type": "qweb",
                "key": "marketing_center_website.cookie_notice",
                "website_id": self.env["website"].search([], limit=1).id,
                "arch_db": "<t><p>Edited copy</p></t>",
            }
        )
        with self.assertRaisesRegex(UserError, "Website copy"):
            self.retire(self.env)

    def test_helper_reference_in_reset_source_is_refused(self):
        view = self.views.create(
            {
                "name": "Native privacy Reset dependency",
                "type": "qweb",
                "arch_db": "<t><p>Native privacy</p></t>",
            }
        )
        view.with_context(no_save_prev=True).write(
            {
                "arch_prev": '<t t-esc="website._marketing_notice_storage_key()"/>',
            }
        )
        with self.assertRaisesRegex(UserError, "references remain"):
            self.retire(self.env)

    def test_unreviewed_owned_notice_digest_is_refused(self):
        view = self.views.create(
            {
                "name": "Edited owned notice",
                "type": "qweb",
                "key": "marketing_center_website.cookie_notice",
                "arch_db": "<t><p>Unreviewed notice</p></t>",
            }
        )
        self.env["ir.model.data"].create(
            {
                "module": "marketing_center_website",
                "name": "cookie_notice",
                "model": "ir.ui.view",
                "res_id": view.id,
            }
        )
        with self.assertRaisesRegex(UserError, "CMS override"):
            self.retire(self.env)

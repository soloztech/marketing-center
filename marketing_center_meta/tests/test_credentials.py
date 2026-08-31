import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

from odoo.tests.common import SavepointCase

from ..services.credentials import MetaCredentialResolutionError, resolve_secret


class TestMetaCredentialResolver(SavepointCase):
    def test_environment_reference_resolves_without_echoing_values(self):
        with patch.dict(
            os.environ,
            {"ODOO_META_READER_TOKEN": "synthetic-meta-reader-token"},
            clear=False,
        ):
            self.assertEqual(
                resolve_secret("environment", "ODOO_META_READER_TOKEN"),
                "synthetic-meta-reader-token",
            )
        with self.assertRaises(MetaCredentialResolutionError) as caught:
            resolve_secret("environment", "invalid-reference")
        self.assertNotIn("invalid-reference", str(caught.exception))

    def test_file_backend_requires_contained_private_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / "reader-token"
            secret.write_text("synthetic-meta-reader-token", encoding="utf-8")
            secret.chmod(stat.S_IRUSR | stat.S_IWUSR)
            with patch.dict(
                os.environ,
                {"ODOO_META_API_SECRET_DIR": directory},
                clear=False,
            ):
                self.assertEqual(
                    resolve_secret("file", "reader-token"),
                    "synthetic-meta-reader-token",
                )
                secret.chmod(stat.S_IRUSR | stat.S_IRGRP)
                with self.assertRaises(MetaCredentialResolutionError):
                    resolve_secret("file", "reader-token")
                with self.assertRaises(MetaCredentialResolutionError):
                    resolve_secret("file", "../reader-token")

    def test_missing_file_backend_root_uses_safe_error_taxonomy(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "missing")
            with patch.dict(
                os.environ,
                {"ODOO_META_API_SECRET_DIR": missing},
                clear=False,
            ), self.assertRaises(MetaCredentialResolutionError) as caught:
                resolve_secret("file", "reader-token")
        self.assertEqual(
            str(caught.exception),
            "Meta credential backend is unavailable",
        )
        self.assertIsNone(caught.exception.__cause__)

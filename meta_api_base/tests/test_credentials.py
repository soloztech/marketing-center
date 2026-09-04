import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

from odoo.tests.common import SavepointCase

from ..services.credentials import (
    MetaCredentialResolutionError,
    resolve_secret,
    validate_secret_reference,
)


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

    def test_environment_secret_rejects_control_characters(self):
        for value in (
            "synthetic-meta\x00secret",
            "synthetic-meta\x7fsecret",
        ):
            # POSIX rejects NUL before it reaches a real environment. Patch the
            # mapping read boundary so both invalid values exercise our resolver.
            with self.subTest(value=repr(value)), patch.object(
                os.environ,
                "get",
                return_value=value,
            ), self.assertRaises(MetaCredentialResolutionError):
                resolve_secret("environment", "ODOO_META_READER_TOKEN")

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

    def test_file_backend_rejects_symlink_and_non_directory_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / "actual-token"
            secret.write_text("synthetic-meta-reader-token", encoding="utf-8")
            secret.chmod(stat.S_IRUSR | stat.S_IWUSR)
            link = root / "reader-token"
            link.symlink_to(secret)
            with patch.dict(
                os.environ,
                {"ODOO_META_API_SECRET_DIR": directory},
                clear=False,
            ), self.assertRaises(MetaCredentialResolutionError) as caught:
                resolve_secret("file", "reader-token")
            self.assertEqual(str(caught.exception), "Meta credential file is unsafe")

            with patch.dict(
                os.environ,
                {"ODOO_META_API_SECRET_DIR": str(secret)},
                clear=False,
            ), self.assertRaises(MetaCredentialResolutionError) as caught:
                resolve_secret("file", "reader-token")
            self.assertEqual(
                str(caught.exception),
                "Meta credential backend is unavailable",
            )

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

    def test_reference_validation_is_backend_specific(self):
        self.assertEqual(
            validate_secret_reference("environment", "ODOO_META_APP_SECRET"),
            "ODOO_META_APP_SECRET",
        )
        self.assertEqual(
            validate_secret_reference("file", "meta-app-secret.v1"),
            "meta-app-secret.v1",
        )
        for backend, reference in (
            ("environment", "lowercase"),
            ("file", "../escape"),
            ("vault", "META_APP_SECRET"),
        ):
            with self.subTest(backend=backend), self.assertRaises(
                MetaCredentialResolutionError
            ):
                validate_secret_reference(backend, reference)

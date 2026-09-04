import json
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

from odoo.tests.common import SavepointCase

from ..services.credentials import (
    GoogleCredentialResolutionError,
    _validated_value,
    resolve_credential,
    resolve_service_account_info,
    validate_credential_reference,
)


class TestGoogleCredentialResolver(SavepointCase):
    @staticmethod
    def _service_account_payload(**overrides):
        values = {
            "type": "service_account",
            "project_id": "soloz-reader",
            "private_key_id": "a" * 40,
            "private_key": "-----BEGIN PRIVATE KEY-----\n%s\n-----END PRIVATE KEY-----\n"
            % ("A" * 512),
            "client_email": "reader@soloz-reader.iam.gserviceaccount.com",
            "client_id": "123456789012345678901",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        values.update(overrides)
        return values

    def test_environment_reference_resolves_without_echoing_values(self):
        with patch.dict(
            os.environ,
            {"ODOO_GOOGLE_DEVELOPER_TOKEN": "synthetic-developer-token"},
            clear=False,
        ):
            self.assertEqual(
                resolve_credential("environment", "ODOO_GOOGLE_DEVELOPER_TOKEN"),
                "synthetic-developer-token",
            )
        with self.assertRaises(GoogleCredentialResolutionError) as caught:
            resolve_credential("environment", "invalid-reference")
        self.assertNotIn("invalid-reference", str(caught.exception))

    def test_file_backend_requires_contained_private_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credential = root / "google-refresh-token"
            credential.write_text("synthetic-refresh-token", encoding="utf-8")
            credential.chmod(stat.S_IRUSR | stat.S_IWUSR)
            with patch.dict(
                os.environ,
                {"ODOO_GOOGLE_API_SECRET_DIR": directory},
                clear=False,
            ):
                self.assertEqual(
                    resolve_credential("file", "google-refresh-token"),
                    "synthetic-refresh-token",
                )
                credential.chmod(stat.S_IRUSR | stat.S_IRGRP)
                with self.assertRaises(GoogleCredentialResolutionError):
                    resolve_credential("file", "google-refresh-token")
                with self.assertRaises(GoogleCredentialResolutionError):
                    resolve_credential("file", "../google-refresh-token")

    def test_file_backend_rejects_symlink_and_non_directory_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actual = root / "actual-token"
            actual.write_text("synthetic-refresh-token", encoding="utf-8")
            actual.chmod(stat.S_IRUSR | stat.S_IWUSR)
            link = root / "google-refresh-token"
            link.symlink_to(actual)
            with patch.dict(
                os.environ,
                {"ODOO_GOOGLE_API_SECRET_DIR": directory},
                clear=False,
            ), self.assertRaises(GoogleCredentialResolutionError) as caught:
                resolve_credential("file", "google-refresh-token")
            self.assertEqual(str(caught.exception), "Google credential file is unsafe")

            with patch.dict(
                os.environ,
                {"ODOO_GOOGLE_API_SECRET_DIR": str(actual)},
                clear=False,
            ), self.assertRaises(GoogleCredentialResolutionError) as caught:
                resolve_credential("file", "google-refresh-token")
            self.assertEqual(
                str(caught.exception), "Google credential backend is unavailable"
            )

    def test_reference_and_value_validation_fail_closed(self):
        self.assertEqual(
            validate_credential_reference(
                "environment", "ODOO_GOOGLE_OAUTH_CLIENT_SECRET"
            ),
            "ODOO_GOOGLE_OAUTH_CLIENT_SECRET",
        )
        self.assertEqual(
            validate_credential_reference("file", "google-client-secret.v1"),
            "google-client-secret.v1",
        )
        for backend, reference in (
            ("environment", "lowercase"),
            ("file", "../escape"),
            ("vault", "GOOGLE_SECRET"),
        ):
            with self.subTest(backend=backend), self.assertRaises(
                GoogleCredentialResolutionError
            ):
                validate_credential_reference(backend, reference)
        with patch.dict(
            os.environ,
            {"ODOO_GOOGLE_SHORT": "short"},
            clear=False,
        ), self.assertRaises(GoogleCredentialResolutionError):
            resolve_credential("environment", "ODOO_GOOGLE_SHORT")

        with self.assertRaises(GoogleCredentialResolutionError) as caught:
            _validated_value("secret\ud800value")
        self.assertIsNone(caught.exception.__context__)

    def test_invalid_file_bytes_do_not_survive_in_exception_context(self):
        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory) / "invalid-token"
            credential.write_bytes(b"secret-prefix-\xff-secret-suffix")
            credential.chmod(stat.S_IRUSR | stat.S_IWUSR)
            with patch.dict(
                os.environ,
                {"ODOO_GOOGLE_API_SECRET_DIR": directory},
                clear=False,
            ), self.assertRaises(GoogleCredentialResolutionError) as caught:
                resolve_credential("file", "invalid-token")
        self.assertIsNone(caught.exception.__context__)

    def test_service_account_json_is_bounded_private_and_strict(self):
        payload = self._service_account_payload()
        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory) / "google-reader.json"
            credential.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            credential.chmod(stat.S_IRUSR | stat.S_IWUSR)
            with patch.dict(
                os.environ,
                {"ODOO_GOOGLE_API_SECRET_DIR": directory},
                clear=False,
            ):
                resolved = resolve_service_account_info("file", "google-reader.json")
        self.assertEqual(resolved["client_email"], payload["client_email"])
        self.assertNotIn(payload["private_key"], repr(resolved.keys()))

        invalid_payloads = (
            self._service_account_payload(type="authorized_user"),
            self._service_account_payload(token_uri="https://attacker.invalid/token"),
            self._service_account_payload(universe_domain="attacker.invalid"),
            self._service_account_payload(
                private_key="-----BEGIN PRIVATE KEY-----\n%s\ud800\n"
                "-----END PRIVATE KEY-----\n" % ("A" * 512)
            ),
            {**self._service_account_payload(), "unexpected": "field"},
        )
        for invalid in invalid_payloads:
            with self.subTest(payload=invalid), patch.dict(
                os.environ,
                {"ODOO_GOOGLE_SERVICE_ACCOUNT_JSON": json.dumps(invalid)},
                clear=False,
            ), self.assertRaises(GoogleCredentialResolutionError) as caught:
                resolve_service_account_info(
                    "environment", "ODOO_GOOGLE_SERVICE_ACCOUNT_JSON"
                )
            self.assertNotIn(invalid.get("private_key", ""), str(caught.exception))
            self.assertIsNone(caught.exception.__context__)

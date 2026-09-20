from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workflowy_importer.credentials import CredentialError, load_api_key


class CredentialProviderTests(unittest.TestCase):
    def test_systemd_credential_has_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = directory / "workflowy-api-key"
            path.write_text("systemd-secret\n", encoding="utf-8")
            path.chmod(0o600)
            with mock.patch.dict(
                os.environ,
                {"CREDENTIALS_DIRECTORY": temp, "WORKFLOWY_API_KEY": "env-secret"},
                clear=False,
            ), mock.patch(
                "workflowy_importer.credentials._secret_service_lookup",
                return_value="service-secret",
            ):
                self.assertEqual("systemd-secret", load_api_key())

    def test_secret_service_precedes_environment_and_legacy_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            legacy = Path(temp) / "legacy"
            legacy.write_text("legacy-secret", encoding="utf-8")
            legacy.chmod(0o600)
            with mock.patch.dict(
                os.environ,
                {"CREDENTIALS_DIRECTORY": "", "WORKFLOWY_API_KEY": "env-secret"},
                clear=False,
            ), mock.patch(
                "workflowy_importer.credentials._secret_service_lookup",
                return_value="service-secret",
            ):
                self.assertEqual("service-secret", load_api_key(secret_file=legacy))

    def test_explicit_provider_fails_closed(self) -> None:
        with mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": ""}, clear=False), mock.patch(
            "workflowy_importer.credentials._secret_service_lookup",
            return_value=None,
        ):
            with self.assertRaises(CredentialError):
                load_api_key(provider="secret-service")

    def test_legacy_file_rejects_group_or_other_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            legacy = Path(temp) / "legacy"
            legacy.write_text("legacy-secret", encoding="utf-8")
            legacy.chmod(0o640)
            with self.assertRaises(CredentialError):
                load_api_key(provider="legacy-file", secret_file=legacy)

    def test_environment_can_be_selected_explicitly(self) -> None:
        with mock.patch.dict(os.environ, {"WORKFLOWY_API_KEY": "env-secret"}, clear=False):
            self.assertEqual("env-secret", load_api_key(provider="env"))


if __name__ == "__main__":
    unittest.main()

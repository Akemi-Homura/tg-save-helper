from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.config import load_config


class ConfigPanelPasswordFileTest(unittest.TestCase):
    def _base_env(self, tmp: str) -> dict[str, str]:
        return {
            "TG_API_ID": "123",
            "TG_API_HASH": "hash",
            "TG_SESSION_NAME": str(Path(tmp) / "session"),
            "TG_DATABASE_PATH": str(Path(tmp) / "db.sqlite3"),
            "TG_SAVED_MEDIA_PATH": str(Path(tmp) / "saved"),
            "TG_PANEL_ENABLED": "1",
            "TG_PANEL_USERNAME": "quals",
        }

    def test_panel_password_can_be_loaded_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            password_file = Path(tmp) / "panel-password"
            password_file.write_text("secret-from-file\n")
            env = self._base_env(tmp) | {"TG_PANEL_PASSWORD_FILE": str(password_file)}
            with patch.dict(os.environ, env, clear=True), patch("src.config.load_dotenv"):
                config = load_config()

        self.assertEqual(config.panel_username, "quals")
        self.assertEqual(config.panel_password, "secret-from-file")
        self.assertEqual(config.panel_password_file, password_file)

    def test_direct_panel_password_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            password_file = Path(tmp) / "panel-password"
            password_file.write_text("secret-from-file\n")
            env = self._base_env(tmp) | {
                "TG_PANEL_PASSWORD": "direct-secret",
                "TG_PANEL_PASSWORD_FILE": str(password_file),
            }
            with patch.dict(os.environ, env, clear=True), patch("src.config.load_dotenv"):
                config = load_config()

        self.assertEqual(config.panel_password, "direct-secret")

    def test_missing_panel_password_file_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = self._base_env(tmp) | {
                "TG_PANEL_PASSWORD_FILE": str(Path(tmp) / "missing")
            }
            with patch.dict(os.environ, env, clear=True), patch("src.config.load_dotenv"):
                with self.assertRaisesRegex(ValueError, "Cannot read TG_PANEL_PASSWORD_FILE"):
                    load_config()


if __name__ == "__main__":
    unittest.main()

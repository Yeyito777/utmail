from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def run_cli(self, *args, env=None):
        merged = os.environ.copy()
        merged["PYTHONPATH"] = str(ROOT / "src")
        if env:
            merged.update(env)
        return subprocess.run(
            [sys.executable, "-m", "utmail_tool.cli", *args],
            cwd=ROOT,
            env=merged,
            text=True,
            capture_output=True,
        )

    def test_help_and_version(self):
        self.assertEqual(self.run_cli("--help").returncode, 0)
        version = self.run_cli("--version")
        self.assertEqual(version.returncode, 0)
        self.assertIn("0.3.1", version.stdout)

    def test_persistent_login_requires_explicit_mailbox_without_environment_default(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "browser-profile"
            result = self.run_cli(
                "login", "--persistent", "--json",
                env={
                    "UTMAIL_MAILBOX": "",
                    "UTMAIL_BROWSER_PROFILE": str(profile),
                },
            )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stderr)
        self.assertEqual(payload["error"]["kind"], "UsageError")
        self.assertFalse(profile.exists())

    def test_unconfigured_json_error_is_stable_and_contains_no_token(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli(
                "status", "--json",
                env={
                    "UTMAIL_SESSION_FILE": str(Path(directory) / "missing.json"),
                    "UTMAIL_SESSION_LOCK_FILE": str(Path(directory) / "lock"),
                },
            )
        self.assertEqual(result.returncode, 3)
        payload = json.loads(result.stderr)
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["error"]["kind"], "SessionRequiredError")
        self.assertNotIn("access_token", result.stderr)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from helpers import token
from utmail_tool.errors import SessionRequiredError
from utmail_tool.session import OwaSession, delete_session, load_session, save_session


class SessionStorageTests(unittest.TestCase):
    def test_token_validation_and_public_view_never_expose_secret(self):
        raw = token(marker="do-not-print-this")
        value = OwaSession.from_token(raw, mailbox="student@example.edu", source="stdin")
        public = json.dumps(value.public())
        self.assertNotIn(raw, public)
        self.assertNotIn("do-not-print-this", public)
        self.assertEqual(value.mailbox, "student@example.edu")

    def test_expired_token_is_rejected(self):
        with self.assertRaises(SessionRequiredError):
            OwaSession.from_token(
                token(expires=int(time.time()) - 1),
                mailbox="student@example.edu",
                source="stdin",
            )

    def test_persistent_session_public_view_redacts_both_credentials(self):
        access = token(marker="access-secret")
        refresh = "refresh-secret-" + "r" * 256
        value = OwaSession.from_persistent_tokens(
            access,
            refresh,
            refresh_token_expires_at=int(time.time()) + 86_400,
            mailbox="student@example.edu",
        )
        public = json.dumps(value.public())
        self.assertNotIn(access, public)
        self.assertNotIn(refresh, public)
        self.assertTrue(value.public()["automaticRenewal"])
        self.assertEqual(value.public()["renewalMode"], "persistent-browser")

    def test_private_atomic_round_trip_and_logout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, lock = root / "state" / "session.json", root / "state" / "session.lock"
            value = OwaSession.from_token(token(), mailbox="student@example.edu", source="stdin")
            save_session(value, path=path, lock=lock)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            old = os.environ.get("UTMAIL_SESSION_FILE")
            os.environ["UTMAIL_SESSION_FILE"] = str(path)
            try:
                self.assertEqual(load_session().object_id, "object-id")
            finally:
                if old is None:
                    os.environ.pop("UTMAIL_SESSION_FILE", None)
                else:
                    os.environ["UTMAIL_SESSION_FILE"] = old
            self.assertTrue(delete_session(path=path, lock=lock))
            self.assertFalse(path.exists())

from __future__ import annotations

import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

from helpers import token
from utmail_tool.persistent_browser import delete_browser_profile
from utmail_tool.renewal import load_or_refresh_session, refresh_direct
from utmail_tool.errors import SessionRequiredError
from utmail_tool.session import OwaSession, save_session


class FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self.content = json.dumps(payload).encode()
        self.closed = False

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class FakeBrowserAuthenticator:
    def __init__(self, result: OwaSession):
        self.result = result
        self.calls = []

    def acquire(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class RenewalTests(unittest.TestCase):
    def persistent_session(self, *, expired_access: bool = False, expired_refresh: bool = False):
        access = token(expires=int(time.time()) - 1 if expired_access else None)
        # Construct from a current token first because imported expired access is rejected.
        session = OwaSession.from_persistent_tokens(
            token(),
            "r" * 512,
            refresh_token_expires_at=int(time.time()) + 86_400,
            mailbox="student@example.edu",
        )
        if expired_access:
            data = {**session.__dict__, "access_token": access, "expires_at": int(time.time()) - 1}
            session = OwaSession.from_dict(data)
        if expired_refresh:
            data = {**session.__dict__, "refresh_token_expires_at": int(time.time()) - 1}
            session = OwaSession.from_dict(data)
        return session

    def test_direct_refresh_rotates_credentials_without_putting_them_in_url_or_headers(self):
        old = self.persistent_session(expired_access=True)
        fresh_access = token(marker="new-access")
        response = FakeResponse(200, {
            "access_token": fresh_access,
            "refresh_token": "n" * 512,
            "expires_in": 3600,
        })
        transport = FakeTransport(response)
        renewed = refresh_direct(old, transport=transport)
        self.assertEqual(renewed.access_token, fresh_access)
        self.assertEqual(renewed.refresh_token, "n" * 512)
        self.assertEqual(renewed.refresh_token_expires_at, old.refresh_token_expires_at)
        url, kwargs = transport.calls[0]
        self.assertNotIn(old.refresh_token, url)
        self.assertNotIn(old.refresh_token, json.dumps(kwargs["headers"]))
        self.assertEqual(kwargs["data"]["refresh_token"], old.refresh_token)
        self.assertFalse(kwargs["allow_redirects"])

    def test_expired_access_is_renewed_atomically_by_direct_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, lock = root / "session.json", root / "session.lock"
            old = self.persistent_session(expired_access=True)
            save_session(old, path=path, lock=lock)
            fresh = old.with_refreshed_tokens(token(marker="fresh"), "x" * 512)
            result = load_or_refresh_session(
                path=path,
                lock=lock,
                direct_refresher=lambda _: fresh,
            )
            self.assertEqual(result.access_token, fresh.access_token)
            self.assertNotIn(old.refresh_token, result.public().values())

    def test_browser_session_recovers_after_hard_refresh_window_ends(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, lock = root / "session.json", root / "session.lock"
            old = self.persistent_session(expired_access=True, expired_refresh=True)
            save_session(old, path=path, lock=lock)
            fresh = OwaSession.from_persistent_tokens(
                token(marker="browser-fresh"),
                "b" * 512,
                refresh_token_expires_at=int(time.time()) + 86_400,
                mailbox="student@example.edu",
            )
            browser = FakeBrowserAuthenticator(fresh)
            result = load_or_refresh_session(
                path=path,
                lock=lock,
                browser_authenticator=browser,
            )
            self.assertEqual(result.access_token, fresh.access_token)
            self.assertEqual(browser.calls, [{"mailbox": old.mailbox, "interactive": False}])

    def test_rejected_rotating_credential_falls_back_to_owned_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, lock = root / "session.json", root / "session.lock"
            old = self.persistent_session(expired_access=True)
            save_session(old, path=path, lock=lock)
            fresh = OwaSession.from_persistent_tokens(
                token(marker="after-revocation"),
                "b" * 512,
                refresh_token_expires_at=int(time.time()) + 86_400,
                mailbox="student@example.edu",
            )
            browser = FakeBrowserAuthenticator(fresh)

            def rejected(_):
                raise SessionRequiredError("simulated invalid_grant")

            result = load_or_refresh_session(
                path=path,
                lock=lock,
                direct_refresher=rejected,
                browser_authenticator=browser,
            )
            self.assertEqual(result.access_token, fresh.access_token)
            self.assertEqual(len(browser.calls), 1)

    def test_concurrent_expiry_performs_only_one_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, lock_path = root / "session.json", root / "session.lock"
            old = self.persistent_session(expired_access=True)
            save_session(old, path=path, lock=lock_path)
            fresh = old.with_refreshed_tokens(token(marker="one-rotation"), "x" * 512)
            calls = 0
            calls_lock = Lock()

            def rotate(_):
                nonlocal calls
                with calls_lock:
                    calls += 1
                time.sleep(0.05)
                return fresh

            def load():
                return load_or_refresh_session(
                    path=path,
                    lock=lock_path,
                    direct_refresher=rotate,
                ).access_token

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: load(), range(2)))
            self.assertEqual(results, [fresh.access_token, fresh.access_token])
            self.assertEqual(calls, 1)

    def test_private_browser_profile_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile"
            profile.mkdir(mode=0o700)
            (profile / "cookie-state").write_text("secret")
            self.assertTrue(delete_browser_profile(profile=profile))
            self.assertFalse(profile.exists())
            self.assertFalse(delete_browser_profile(profile=profile))


if __name__ == "__main__":
    unittest.main()

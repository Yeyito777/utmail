from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import token
from utmail_tool import autologin
from utmail_tool.autologin import Assistant, attempt
from utmail_tool.errors import SessionRequiredError
from utmail_tool.renewal import load_or_refresh_session
from utmail_tool.session import OwaSession, save_session
import test_renewal


WEBLOGIN = "https://idpz.utorauth.utoronto.ca/idp/profile/SAML2/Redirect/SSO?execution=e1s1"
DUO = "https://api-0123abcd.duosecurity.com/frame/v4/auth/prompt?sid=x"
SECRET = "hunter2-Password"


class FakeRunner:
    """Scripted vimbrowser/duo/secrets; records every argv, stdin and env."""

    def __init__(
        self,
        *,
        pages: list[dict] | None = None,
        forms: list[dict] | None = None,
        url: str = WEBLOGIN,
        duo_exit: int = 0,
        begin_exit: int = 0,
    ):
        self.pages = list(pages or [])
        self.forms = list(forms or [])
        self.url = url
        self.duo_exit = duo_exit
        self.begin_exit = begin_exit
        self.calls: list[tuple[list[str], str | None, dict | None]] = []
        self.scripts: list[str] = []

    def __call__(self, argv, *, input=None, env=None, **_kwargs):
        self.calls.append((list(argv), input, env))
        out, code = "", 0
        if argv[0] == "secrets":
            out = json.dumps(["lineroau", SECRET])
        elif argv[:2] == ["duo", "own-login"]:
            if argv[2] == "begin":
                code = self.begin_exit
                out = json.dumps({"ownLogin": {"ticket": "a" * 32}}) if not code else ""
            elif argv[2] == "approve":
                code = self.duo_exit
        elif argv[1] == "tabs":
            out = json.dumps({"tabs": [{"id": 7, "url": self.url}]})
        elif argv[1] == "frame-tree":
            out = json.dumps({"main_frame_id": "F"})
        elif argv[1] == "frame-js":
            self.scripts.append(input)
            if "trust-browser-button')" in input and "codes:" in input:
                result = self.pages.pop(0) if self.pages else {"ok": True, "codes": []}
            elif "form:" in input:
                result = self.forms.pop(0) if self.forms else {"form": True}
            else:
                result = {"ok": True}
            out = json.dumps({"result": json.dumps(result)})
        return subprocess.CompletedProcess(argv, code, out, "Error: refused for test" if code else "")

    def argv_text(self) -> str:
        return " ".join(" ".join(argv) for argv, _stdin, _env in self.calls)


class AssistantTests(unittest.TestCase):
    def assistant(self, runner: FakeRunner) -> Assistant:
        return Assistant("a" * 32, "lineroau", SECRET, runner=runner, vimbrowser="vimbrowser-cli")

    def test_weblogin_is_filled_once_through_stdin_never_argv(self) -> None:
        runner = FakeRunner()
        helper = self.assistant(runner)
        helper.step(7)
        self.assertTrue(helper.submitted)
        fills = [s for s in runner.scripts if "login-btn').click" in s or "b.click()" in s]
        self.assertEqual(len(fills), 1)
        self.assertIn(json.dumps(SECRET), fills[0])
        self.assertNotIn(SECRET, runner.argv_text())
        # The form showing again means the password was rejected: never retry.
        with self.assertRaisesRegex(SessionRequiredError, "did not accept"):
            helper.step(7)

    def test_old_form_while_the_submit_is_in_flight_is_not_a_rejection(self) -> None:
        # Oct 14: the poll after submit still saw the old form while its POST was loading.
        runner = FakeRunner(forms=[{"form": True}, {"form": True, "pending": True}, {"form": True}])
        helper = self.assistant(runner)
        helper.step(7)
        fill = next(s for s in runner.scripts if "b.click()" in s)
        self.assertLess(fill.index("data-exo-submitted"), fill.index("b.click()"))
        helper.step(7)  # same document, still submitting: wait
        self.assertEqual(sum("b.click()" in s for s in runner.scripts), 1)
        with self.assertRaisesRegex(SessionRequiredError, "did not accept"):
            helper.step(7)  # a fresh form after the submit means it was rejected

    def test_duo_code_on_our_device_is_approved_once_with_code_on_stdin(self) -> None:
        runner = FakeRunner(url=DUO, pages=[
            {"ok": True, "codes": []},
            {"ok": True, "codes": ["295"], "sentTo": "Sent to Exocortex local (iOS)"},
            {"ok": True, "codes": ["295"], "sentTo": "Sent to Exocortex local (iOS)", "trust": True},
        ])
        helper = self.assistant(runner)
        helper.step(7)
        self.assertFalse(helper.approved)
        helper.step(7)
        approvals = [(argv, stdin, env) for argv, stdin, env in runner.calls if argv[:3] == ["duo", "own-login", "approve"]]
        self.assertEqual(len(approvals), 1)
        argv, stdin, env = approvals[0]
        self.assertEqual(argv, ["duo", "own-login", "approve", "a" * 32, "--json"])
        self.assertEqual(stdin, "295")
        self.assertEqual(env["UOFT_UTORID"], "lineroau")
        self.assertNotIn("295", runner.argv_text())
        self.assertTrue(any('"remember"' in s for s in runner.scripts))
        helper.step(7)  # trust prompt after approval: click, but never approve again
        self.assertTrue(helper.trusted)
        self.assertEqual(sum(1 for a, _s, _e in runner.calls if a[:3] == ["duo", "own-login", "approve"]), 1)

    def test_push_sent_elsewhere_switches_to_our_device_before_approving(self) -> None:
        runner = FakeRunner(url=DUO, pages=[
            {"ok": True, "codes": ["111"], "sentTo": "Sent to iPhone (iOS)"},
            {"ok": True, "codes": []},
            {"ok": True, "codes": ["295"], "sentTo": "Sent to Exocortex local (iOS)"},
        ])
        helper = self.assistant(runner)
        helper.step(7)
        helper.step(7)
        self.assertEqual(helper.switch_stage, 2)
        self.assertFalse(helper.approved)
        helper.step(7)
        self.assertTrue(helper.approved)
        self.assertTrue(any('"other-options"' in s for s in runner.scripts))
        self.assertTrue(any('"pick-device"' in s for s in runner.scripts))

    def test_duo_refusal_and_ambiguous_code_raise_session_required(self) -> None:
        refused = FakeRunner(url=DUO, duo_exit=1, pages=[{"ok": True, "codes": ["295"], "sentTo": "Sent to Exocortex local (iOS)"}])
        with self.assertRaisesRegex(SessionRequiredError, "refused for test"):
            self.assistant(refused).step(7)
        ambiguous = FakeRunner(url=DUO, pages=[{"ok": True, "codes": ["295", "296"], "sentTo": "Sent to Exocortex local (iOS)"}])
        with self.assertRaisesRegex(SessionRequiredError, "ambiguous"):
            self.assistant(ambiguous).step(7)

    def test_other_hosts_are_ignored(self) -> None:
        runner = FakeRunner(url="https://login.microsoftonline.com/common/oauth2")
        self.assistant(runner).step(7)
        self.assertEqual(runner.scripts, [])


class AttemptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.marker = Path(self.temporary.name) / "autologin.json"
        self.original = SessionRequiredError("needs human")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_attempt(self, runner: FakeRunner, login, *, now: float = 10_000.0):
        return attempt(login, self.original, runner=runner, clock=lambda: now, marker=self.marker)

    def test_success_ends_ticket_completed_and_returns_login_result(self) -> None:
        runner = FakeRunner()
        seen = []
        result = self.run_attempt(runner, lambda helper: seen.append(helper) or "session")
        self.assertEqual(result, "session")
        self.assertEqual(seen[0].ticket, "a" * 32)
        self.assertEqual(runner.calls[-1][0], ["duo", "own-login", "end", "a" * 32, "--outcome", "completed"])
        self.assertNotIn(SECRET, runner.argv_text())

    def test_failure_ends_ticket_failed_and_keeps_session_required(self) -> None:
        runner = FakeRunner()

        def login(_helper):
            raise SessionRequiredError("timed out")

        with self.assertRaisesRegex(SessionRequiredError, "needs human; automatic re-login failed: timed out"):
            self.run_attempt(runner, login)
        self.assertEqual(runner.calls[-1][0][-1], "failed")

    def test_termination_mid_login_still_ends_the_ticket(self) -> None:
        # A caller's `timeout` sends SIGTERM; the ticket must still be released.
        runner = FakeRunner()
        previous = signal.getsignal(signal.SIGTERM)
        try:
            with self.assertRaises(SystemExit):
                self.run_attempt(runner, lambda _helper: os.kill(os.getpid(), signal.SIGTERM))
        finally:
            signal.signal(signal.SIGTERM, previous)
        self.assertEqual(runner.calls[-1][0], ["duo", "own-login", "end", "a" * 32, "--outcome", "failed"])

    def test_rate_limit_and_begin_refusal_skip_the_browser(self) -> None:
        refused = FakeRunner(begin_exit=1)
        with self.assertRaisesRegex(SessionRequiredError, "not started"):
            self.run_attempt(refused, lambda _h: self.fail("must not log in"))
        again = FakeRunner()
        with self.assertRaisesRegex(SessionRequiredError, "skipped"):
            self.run_attempt(again, lambda _h: self.fail("must not log in"), now=10_000.0 + 60)
        self.assertEqual(again.calls, [])
        self.assertEqual(self.run_attempt(FakeRunner(), lambda _h: "ok", now=10_000.0 + 31 * 60), "ok")

    def test_can_be_disabled(self) -> None:
        with patch.dict("os.environ", {"UTMAIL_AUTOLOGIN": "0"}):
            with self.assertRaises(SessionRequiredError) as caught:
                self.run_attempt(FakeRunner(), lambda _h: "ok")
        self.assertIs(caught.exception, self.original)


class RenewalHookTests(unittest.TestCase):
    def test_expired_browser_renewal_falls_back_to_one_auto_login(self) -> None:
        expired = test_renewal.RenewalTests.persistent_session(self, expired_access=True, expired_refresh=True)
        fresh = OwaSession.from_persistent_tokens(
            token(marker="auto"), "b" * 512, refresh_token_expires_at=int(time.time()) + 86_400, mailbox="student@example.edu"
        )

        class Browser:
            calls: list[dict] = []

            def acquire(self, **kwargs):
                self.calls.append(kwargs)
                if not kwargs["interactive"]:
                    raise SessionRequiredError("needs human")
                return fresh

        browser = Browser()
        used = []

        def auto_login(login, original):
            used.append(original.message)
            return login("assistant")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            lock = Path(directory) / "session.lock"
            save_session(expired, path=path, lock=lock)
            result = load_or_refresh_session(
                path=path, lock=lock, browser_authenticator=browser, auto_login=auto_login
            )
        self.assertEqual(result.access_token, fresh.access_token)
        self.assertEqual(used, ["needs human"])
        self.assertEqual([c["interactive"] for c in browser.calls], [False, True])
        self.assertEqual(browser.calls[1]["assistant"], "assistant")


if __name__ == "__main__":
    unittest.main()

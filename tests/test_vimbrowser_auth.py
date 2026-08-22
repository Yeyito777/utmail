from __future__ import annotations

import json
import subprocess
import time
import unittest

from helpers import token
from utmail_tool.errors import BrowserImportError, SessionRequiredError
from utmail_tool.session import OWA_APP_ID
from utmail_tool.vimbrowser import (
    CAPTURE_JS,
    READ_CAPTURE_JS,
    TOKEN_DISCOVERY_JS,
    TRIGGER_JS,
    VimbrowserAuthenticator,
)


class FakeClock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class AuthRunner:
    def __init__(self, *, page_url: str = "https://outlook.cloud.microsoft/mail/"):
        self.page_url = page_url
        self.calls: list[tuple[list[str], str | None, float]] = []
        self.access = token(marker="vimbrowser-access")
        self.refresh = "vimbrowser-refresh-" + "r" * 512

    def __call__(self, command, *, input, text, capture_output, timeout, check):
        self.calls.append((command, input, timeout))
        args = command[1:]
        if args == ["tabs", "--json"]:
            opened = any(call[0][1:2] == ["open-context"] for call in self.calls)
            if opened:
                output = json.dumps({
                    "active_tabid": 9,
                    "tabs": [
                        {"id": 4, "url": "https://example.com/", "active": False},
                        {
                            "id": 9,
                            "url": self.page_url,
                            "active": True,
                            "context": "utmail-helper",
                        },
                    ],
                })
            else:
                output = json.dumps({
                    "active_tabid": 4,
                    "tabs": [{"id": 4, "url": "https://example.com/", "active": True}],
                })
        elif args == ["open-context", "utmail-helper", "https://outlook.cloud.microsoft/mail/"]:
            output = json.dumps({
                "ok": True,
                "active_tabid": 4,
                "tabs": [
                    {"id": 4, "url": "https://example.com/", "active": True},
                    {
                        "id": 9,
                        "url": "https://outlook.cloud.microsoft/mail/",
                        "active": False,
                        "context": "utmail-helper",
                    },
                ],
            })
        elif args == ["frame-tree", "9"]:
            output = json.dumps({"ok": True, "tabid": 9, "main_frame_id": "main"})
        elif args == ["frame-js", "9", "main"]:
            if input != TOKEN_DISCOVERY_JS:
                raise AssertionError("unexpected frame script")
            value = {
                "ok": True,
                "accessToken": self.access,
                "refreshToken": self.refresh,
                "refreshTokenExpiresAt": int(time.time()) + 86_400,
                "clientId": OWA_APP_ID,
                "accountId": "object-id.tenant-id",
            }
            output = json.dumps({"ok": True, "tabid": 9, "type": "string", "result": json.dumps(value)})
        elif args in (["focus", "9"], ["close-tab", "9"], ["focus", "4"]):
            output = "{}"
        else:
            raise AssertionError(f"unexpected command: {args}")
        return subprocess.CompletedProcess(command, 0, output, "")


class VimbrowserAuthenticatorTests(unittest.TestCase):
    def test_persistent_auth_uses_only_new_exact_context_tab_then_restores_focus(self):
        runner = AuthRunner()
        auth = VimbrowserAuthenticator(
            executable="vimbrowser-cli",
            context="utmail-helper",
            runner=runner,
            sleeper=lambda _: None,
        )
        session = auth.acquire(mailbox="student@example.edu", interactive=True)
        self.assertEqual(session.access_token, runner.access)
        self.assertEqual(session.refresh_token, runner.refresh)
        self.assertEqual(session.renewal_mode, "vimbrowser")
        self.assertEqual(session.source, "vimbrowser-context:utmail-helper")

        commands = [call[0][1:] for call in runner.calls]
        self.assertIn(
            ["open-context", "utmail-helper", "https://outlook.cloud.microsoft/mail/"],
            commands,
        )
        self.assertIn(["focus", "9"], commands)
        self.assertIn(["frame-tree", "9"], commands)
        self.assertIn(["frame-js", "9", "main"], commands)
        self.assertEqual(commands[-2:], [["close-tab", "9"], ["focus", "4"]])
        self.assertFalse(any(runner.access in " ".join(command) for command, _, _ in runner.calls))
        self.assertFalse(any(runner.refresh in " ".join(command) for command, _, _ in runner.calls))
        self.assertTrue(all(0 < timeout <= 20 for _, _, timeout in runner.calls))

    def test_sign_in_timeout_closes_only_helper_tab_and_instructs_persistent_login(self):
        clock = FakeClock()
        runner = AuthRunner(page_url="https://login.microsoftonline.com/common/oauth2/authorize")
        auth = VimbrowserAuthenticator(
            context="utmail-helper",
            runner=runner,
            clock=clock,
            sleeper=clock.sleep,
        )
        with self.assertRaises(SessionRequiredError) as caught:
            auth.acquire(
                mailbox="student@example.edu",
                interactive=False,
                timeout_seconds=1.0,
            )
        self.assertIn("utmail login --persistent", caught.exception.message)
        commands = [call[0][1:] for call in runner.calls]
        self.assertEqual(commands[-2:], [["close-tab", "9"], ["focus", "4"]])
        self.assertFalse(any(command[:1] == ["frame-tree"] for command in commands))

    def test_opaque_outlook_cache_falls_back_to_context_renewable_bearer_capture(self):
        class OpaqueCacheRunner(AuthRunner):
            def __init__(self):
                super().__init__()
                self.capture_reads = 0

            def __call__(self, command, **kwargs):
                if command[1:] == ["frame-js", "9", "main"]:
                    script = kwargs["input"]
                    self.calls.append((command, script, kwargs["timeout"]))
                    if script == TOKEN_DISCOVERY_JS:
                        value = {"ok": False, "reason": "credential-count"}
                    elif script == CAPTURE_JS:
                        value = {"ok": True}
                    elif script == TRIGGER_JS:
                        value = {"ok": True, "target": "Junk Email"}
                    elif script == READ_CAPTURE_JS:
                        self.capture_reads += 1
                        value = "" if self.capture_reads == 1 else f"Bearer {self.access}"
                        return subprocess.CompletedProcess(
                            command,
                            0,
                            json.dumps({"ok": True, "tabid": 9, "result": value}),
                            "",
                        )
                    else:
                        raise AssertionError("unexpected frame script")
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps({"ok": True, "tabid": 9, "result": json.dumps(value)}),
                        "",
                    )
                return super().__call__(command, **kwargs)

        runner = OpaqueCacheRunner()
        auth = VimbrowserAuthenticator(
            executable="vimbrowser-cli",
            context="utmail-helper",
            runner=runner,
            sleeper=lambda _: None,
        )
        session = auth.acquire(mailbox="student@example.edu", interactive=True)
        self.assertEqual(session.access_token, runner.access)
        self.assertIsNone(session.refresh_token)
        self.assertEqual(session.renewal_mode, "vimbrowser")
        self.assertEqual(session.source, "vimbrowser-context:utmail-helper")
        scripts = [stdin for command, stdin, _ in runner.calls if command[1:2] == ["frame-js"]]
        self.assertIn(TOKEN_DISCOVERY_JS, scripts)
        self.assertIn(CAPTURE_JS, scripts)
        self.assertIn(TRIGGER_JS, scripts)
        self.assertIn(READ_CAPTURE_JS, scripts)

    def test_reused_open_context_id_is_rejected_without_closing_existing_tab(self):
        class ReusedRunner:
            def __init__(self):
                self.calls = []

            def __call__(self, command, **kwargs):
                self.calls.append(command[1:])
                if command[1:] == ["tabs", "--json"]:
                    output = json.dumps({
                        "active_tabid": 9,
                        "tabs": [{"id": 9, "url": "https://example.com/", "active": True}],
                    })
                elif command[1:2] == ["open-context"]:
                    output = json.dumps({
                        "active_tabid": 9,
                        "tabs": [{"id": 9, "url": "https://example.com/", "active": True}],
                    })
                elif command[1:] == ["focus", "9"]:
                    output = "{}"
                else:
                    raise AssertionError(command)
                return subprocess.CompletedProcess(command, 0, output, "")

        runner = ReusedRunner()
        auth = VimbrowserAuthenticator(context="utmail-helper", runner=runner)
        with self.assertRaises(BrowserImportError):
            auth.acquire(mailbox="student@example.edu", interactive=True)
        self.assertFalse(any(command[:1] == ["close-tab"] for command in runner.calls))

    def test_invalid_context_is_rejected_before_any_runner_call(self):
        calls = []
        with self.assertRaises(BrowserImportError):
            VimbrowserAuthenticator(context="UTmail helper", runner=lambda *args, **kwargs: calls.append(args))
        with self.assertRaises(BrowserImportError):
            VimbrowserAuthenticator(context="a" * 49, runner=lambda *args, **kwargs: calls.append(args))
        self.assertEqual(calls, [])

    def test_open_response_must_confirm_one_new_exact_https_context_tab(self):
        hostile_responses = [
            {"active_tabid": 4, "tabs": [{"id": 4, "url": "https://example.com/", "context": "utmail-helper"}]},
            {"active_tabid": 4, "tabs": [{"id": 9, "url": "https://outlook.cloud.microsoft/mail/", "context": "other"}]},
            {"active_tabid": 4, "tabs": [{"id": 9, "url": "http://outlook.cloud.microsoft/mail/", "context": "utmail-helper"}]},
            {
                "active_tabid": 4,
                "tabs": [
                    {"id": 9, "url": "https://outlook.cloud.microsoft/mail/", "context": "utmail-helper"},
                    {"id": 10, "url": "https://outlook.cloud.microsoft/mail/", "context": "utmail-helper"},
                ],
            },
        ]

        for opened in hostile_responses:
            with self.subTest(opened=opened):
                class OpenRunner:
                    def __init__(self, response):
                        self.calls = []
                        self.response = response

                    def __call__(self, command, **kwargs):
                        self.calls.append(command[1:])
                        if command[1:] == ["tabs", "--json"]:
                            output = json.dumps({
                                "active_tabid": 4,
                                "tabs": [{"id": 4, "url": "https://example.com/", "active": True}],
                            })
                        elif command[1:2] == ["open-context"]:
                            output = json.dumps(self.response)
                        elif command[1:] == ["focus", "4"]:
                            output = "{}"
                        else:
                            raise AssertionError(command)
                        return subprocess.CompletedProcess(command, 0, output, "")

                runner = OpenRunner(opened)
                auth = VimbrowserAuthenticator(context="utmail-helper", runner=runner)
                with self.assertRaises(BrowserImportError):
                    auth.acquire(mailbox="student@example.edu", interactive=True)
                self.assertFalse(any(command[:1] == ["close-tab"] for command in runner.calls))

    def test_cached_refresh_account_must_match_access_token_identity(self):
        class WrongAccountRunner(AuthRunner):
            def __call__(self, command, **kwargs):
                completed = super().__call__(command, **kwargs)
                if command[1:2] == ["frame-js"]:
                    payload = json.loads(completed.stdout)
                    result = json.loads(payload["result"])
                    result["accountId"] = "different-object.tenant-id"
                    payload["result"] = json.dumps(result)
                    return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
                return completed

        runner = WrongAccountRunner()
        auth = VimbrowserAuthenticator(context="utmail-helper", runner=runner, sleeper=lambda _: None)
        with self.assertRaises(BrowserImportError) as caught:
            auth.acquire(mailbox="student@example.edu", interactive=True)
        self.assertIn("different cached account", caught.exception.message)
        commands = [call[0][1:] for call in runner.calls]
        self.assertEqual(commands[-2:], [["close-tab", "9"], ["focus", "4"]])

    def test_frame_js_failure_never_echoes_credentials_or_process_output(self):
        secret = "secret-from-local-storage-" + "x" * 200

        class FailedRunner(AuthRunner):
            def __call__(self, command, **kwargs):
                if command[1:2] == ["frame-js"]:
                    return subprocess.CompletedProcess(command, 1, secret, secret)
                return super().__call__(command, **kwargs)

        clock = FakeClock()
        runner = FailedRunner()
        auth = VimbrowserAuthenticator(
            context="utmail-helper",
            runner=runner,
            clock=clock,
            sleeper=clock.sleep,
        )
        with self.assertRaises(SessionRequiredError) as caught:
            auth.acquire(
                mailbox="student@example.edu",
                interactive=False,
                timeout_seconds=0.5,
            )
        self.assertNotIn(secret, caught.exception.message)


if __name__ == "__main__":
    unittest.main()

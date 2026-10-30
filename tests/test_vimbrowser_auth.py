from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import unittest

from helpers import token
from utmail_tool.errors import BrowserImportError, SessionRequiredError
from utmail_tool.session import OWA_APP_ID
from utmail_tool.vimbrowser import (
    ADFS_URL,
    CAPTURE_JS,
    OUTLOOK_URL,
    READ_CAPTURE_JS,
    SIGN_IN_SETTLE_POLLS,
    SSO_ERROR_PROBE_JS,
    TOKEN_DISCOVERY_JS,
    TRIGGER_JS,
    VimbrowserAuthenticator,
    adfs_overgrown,
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
        elif args == ["frame-js", "9", "main"] and input == SSO_ERROR_PROBE_JS:
            value = {"host": self.page_url.split("/")[2], "status": 200, "tooLong": False}
            output = json.dumps({"ok": True, "tabid": 9, "result": json.dumps(value)})
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


ADFS = "sts.ad.utoronto.ca"
WEBLOGIN = "idpz.utorauth.utoronto.ca"
ADFS_400_URL = "https://sts.ad.utoronto.ca/adfs/ls/?SAMLRequest=abc&RelayState=def"
WEBLOGIN_URL = "https://idpz.utorauth.utoronto.ca/idp/profile/SAML2/Redirect/SSO?execution=e1s1"
# The 16 cookies found in utmail-helper on Oct 30 2026.
BLOATED_ADFS = [
    "SamlSession", "SamlSession1", "SamlSession2", "SamlSession3", "SamlSession4", "SamlSession5",
    "MSISContext0a1b", "MSISContext2c3d", "MSISAuthenticated", "MSISLoopDetectionCookie",
    "MSISIPSelectionSession", "MSISSignOut", "SamlLogout", "ReturnUrl", "MSISAuthenticated2", "MSISAuth1",
]


class SsoBrowser:
    """A fake vimbrowser with per-host cookies in the helper context.

    A sign-in host answers HTTP 400 while it holds more than ``limit`` cookies;
    ``reload`` past a healthy sign-in host lands on Outlook.
    """

    def __init__(self, url: str, cookies: dict[str, list[str]], *, limit: int = 8, heals: bool = True):
        self.url = url
        self.cookies = {host: list(names) for host, names in cookies.items()}
        self.cookies.setdefault("login.microsoftonline.com", ["ESTSAUTHPERSISTENT", "SignInStateCookie"])
        self.cookies.setdefault("outlook.cloud.microsoft", ["OpenIdConnect.token.v1"])
        self.limit = limit
        self.heals = heals
        self.opened = False
        self.calls: list[list[str]] = []
        self.access = token(marker="vimbrowser-access")

    def host(self, url: str | None = None) -> str:
        return (url or self.url).split("/")[2]

    def rejected(self) -> bool:
        return self.host() in (ADFS, WEBLOGIN) and len(self.cookies.get(self.host(), [])) > self.limit

    def __call__(self, command, *, input, text, capture_output, timeout, check):
        args = command[1:]
        self.calls.append(args)
        if args == ["tabs", "--json"]:
            tabs = [{"id": 4, "url": "https://example.com/", "active": True}]
            if self.opened:
                tabs.append({"id": 9, "url": self.url, "active": False, "loading": False, "context": "utmail-helper"})
            value = {"active_tabid": 4, "tabs": tabs}
        elif args == ["open-context", "utmail-helper", OUTLOOK_URL]:
            self.opened = True
            value = {"tabs": 2, "active_tabid": 4}
        elif args == ["frame-tree", "9"]:
            value = {"tabid": 9, "main_frame_id": "main"}
        elif args == ["frame-js", "9", "main"] and input == SSO_ERROR_PROBE_JS:
            rejected = self.rejected()
            page = {"host": self.host(), "status": 400 if rejected else 200, "tooLong": rejected}
            value = {"ok": True, "tabid": 9, "result": json.dumps(page)}
        elif args == ["frame-js", "9", "main"] and input == TOKEN_DISCOVERY_JS:
            if not self.url.startswith(OUTLOOK_URL):
                page = {"ok": False, "reason": "origin"}
            else:
                page = {
                    "ok": True,
                    "accessToken": self.access,
                    "refreshToken": "vimbrowser-refresh-" + "r" * 512,
                    "refreshTokenExpiresAt": int(time.time()) + 86_400,
                    "clientId": OWA_APP_ID,
                    "accountId": "object-id.tenant-id",
                }
            value = {"ok": True, "tabid": 9, "result": json.dumps(page)}
        elif args[:2] == ["cookies", "9"]:
            host = self.host(args[2] if len(args) > 2 else None)
            rows = [
                {"name": name, "value": "secret-" + name, "domain": host, "path": "/adfs", "httponly": True}
                for name in self.cookies.get(host, [])
            ]
            rows += [
                {"name": name, "value": "secret-" + name, "domain": ".utoronto.ca", "path": "/"}
                for name in self.cookies.get(".utoronto.ca", [])
            ]
            value = {"cookies": rows}
        elif args[:2] == ["cookie-delete", "9"]:
            names = self.cookies.get(self.host(), [])
            value = {"deleted": names.count(args[2])}
            self.cookies[self.host()] = [name for name in names if name != args[2]]
        elif args[:2] == ["load", "9"]:
            self.url = args[2]
        elif args == ["reload", "9"]:
            if self.heals and not self.rejected():
                self.url = OUTLOOK_URL
        elif args in (["focus", "9"], ["close-tab", "9"], ["focus", "4"]):
            value = {}
        else:
            raise AssertionError(f"unexpected command: {args}")
        if args[:1] in (["load"], ["reload"]):
            value = {"ok": True}
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")


class RecordingAssistant:
    def __init__(self):
        self.urls: list[str] = []

    def step(self, tab_id, url):
        self.urls.append(url)


class OversizedSsoCookieTests(unittest.TestCase):
    def authenticator(self, browser, logs):
        clock = FakeClock()
        return VimbrowserAuthenticator(
            context="utmail-helper", runner=browser, clock=clock, sleeper=clock.sleep, log=logs.append
        )

    def test_adfs_header_too_long_clears_only_adfs_cookies_reloads_once_and_finishes(self):
        browser = SsoBrowser(ADFS_400_URL, {ADFS: BLOATED_ADFS})
        logs: list[str] = []
        session = self.authenticator(browser, logs).acquire(mailbox="student@example.edu", interactive=True)
        self.assertEqual(session.access_token, browser.access)
        deleted = [args[2] for args in browser.calls if args[:1] == ["cookie-delete"]]
        self.assertEqual(deleted, BLOATED_ADFS)
        self.assertEqual(browser.cookies[ADFS], [])
        self.assertEqual(browser.cookies["login.microsoftonline.com"], ["ESTSAUTHPERSISTENT", "SignInStateCookie"])
        self.assertEqual(browser.cookies["outlook.cloud.microsoft"], ["OpenIdConnect.token.v1"])
        self.assertEqual(sum(args == ["reload", "9"] for args in browser.calls), 1)
        self.assertEqual(len(logs), 1)
        self.assertIn("Header Field Too Long", logs[0])
        self.assertIn(f"cleared 16 {ADFS} cookies", logs[0])
        self.assertIn("SamlSession5", logs[0])
        self.assertNotIn("secret-", logs[0])

    def test_recovery_runs_once_per_host_and_then_times_out_normally(self):
        browser = SsoBrowser(ADFS_400_URL, {ADFS: BLOATED_ADFS}, heals=False)
        browser.limit = -1  # Still rejected after clearing.
        logs: list[str] = []
        with self.assertRaisesRegex(SessionRequiredError, "utmail login --persistent"):
            self.authenticator(browser, logs).acquire(
                mailbox="student@example.edu", interactive=False, timeout_seconds=5.0
            )
        self.assertEqual(sum(args == ["reload", "9"] for args in browser.calls), 1)
        self.assertEqual(len(logs), 1)
        self.assertEqual(browser.calls[-1], ["close-tab", "9"])

    def test_weblogin_400_clears_only_weblogin_cookies(self):
        weblogin = [f"cookie{index}" for index in range(10)]
        browser = SsoBrowser(WEBLOGIN_URL, {WEBLOGIN: weblogin, ADFS: ["SamlSession", "MSISAuthenticated"]})
        logs: list[str] = []
        self.authenticator(browser, logs).acquire(mailbox="student@example.edu", interactive=True)
        self.assertEqual([args[2] for args in browser.calls if args[:1] == ["cookie-delete"]], weblogin)
        self.assertEqual(browser.cookies[ADFS], ["SamlSession", "MSISAuthenticated"])
        self.assertIn(f"cleared 10 {WEBLOGIN} cookies", logs[0])

    def test_healthy_sign_in_page_is_left_alone(self):
        browser = SsoBrowser(ADFS_400_URL, {ADFS: ["SamlSession", "MSISAuthenticated"]})
        logs: list[str] = []
        with self.assertRaises(SessionRequiredError):
            self.authenticator(browser, logs).acquire(
                mailbox="student@example.edu", interactive=False, timeout_seconds=3.0
            )
        self.assertFalse(any(args[:1] in (["cookie-delete"], ["reload"]) for args in browser.calls))
        self.assertEqual(logs, [])

    def test_names_shared_with_other_domains_or_unsafe_are_never_deleted(self):
        browser = SsoBrowser(ADFS_400_URL, {ADFS: [*BLOATED_ADFS, "bad name"], ".utoronto.ca": ["ReturnUrl"]})
        logs: list[str] = []
        self.authenticator(browser, logs).acquire(mailbox="student@example.edu", interactive=True)
        deleted = [args[2] for args in browser.calls if args[:1] == ["cookie-delete"]]
        self.assertNotIn("ReturnUrl", deleted)
        self.assertNotIn("bad name", deleted)
        self.assertEqual(len(deleted), 15)

    def test_autologin_prunes_overgrown_adfs_cookies_before_signing_in(self):
        browser = SsoBrowser(OUTLOOK_URL, {ADFS: ["SamlSession", "SamlSession1", "SamlSession2", "SamlSession3"]})
        logs: list[str] = []
        assistant = RecordingAssistant()
        self.authenticator(browser, logs).acquire(
            mailbox="student@example.edu", interactive=True, timeout_seconds=150.0, assistant=assistant
        )
        loads = [args[2] for args in browser.calls if args[:1] == ["load"]]
        self.assertEqual(loads, [ADFS_URL, OUTLOOK_URL])
        self.assertEqual(browser.cookies[ADFS], [])
        self.assertIn(["cookies", "9", ADFS_URL], browser.calls)
        self.assertIn("before automatic re-login", logs[0])
        self.assertEqual(assistant.urls, [OUTLOOK_URL])
        self.assertEqual(browser.cookies["login.microsoftonline.com"], ["ESTSAUTHPERSISTENT", "SignInStateCookie"])

    def test_autologin_keeps_a_normal_adfs_cookie_set(self):
        normal = ["SamlSession", "MSISSignOut", "MSISAuthenticated", "MSISLoopDetectionCookie"]
        browser = SsoBrowser(OUTLOOK_URL, {ADFS: normal})
        logs: list[str] = []
        self.authenticator(browser, logs).acquire(
            mailbox="student@example.edu", interactive=True, timeout_seconds=150.0, assistant=RecordingAssistant()
        )
        self.assertFalse(any(args[:1] in (["load"], ["cookie-delete"]) for args in browser.calls))
        self.assertEqual(browser.cookies[ADFS], normal)
        self.assertEqual(logs, [])

    def test_overgrowth_threshold(self):
        self.assertFalse(adfs_overgrown(["SamlSession", "SamlSession1", "SamlSession2", "MSISAuthenticated"]))
        self.assertTrue(adfs_overgrown(["SamlSession", "SamlSession3"]))
        self.assertTrue(adfs_overgrown([f"c{index}" for index in range(9)]))
        self.assertFalse(adfs_overgrown([f"c{index}" for index in range(8)]))


class VimbrowserAuthenticatorTests(unittest.TestCase):
    def test_successful_silent_renewal_never_requests_focus(self):
        runner = AuthRunner()
        auth = VimbrowserAuthenticator(context="utmail-helper", runner=runner)
        session = auth.acquire(mailbox="student@example.edu", interactive=False)
        self.assertEqual(session.renewal_mode, "vimbrowser")
        commands = [call[0][1:] for call in runner.calls]
        self.assertEqual(commands[-1], ["close-tab", "9"])
        self.assertFalse(any(command[0] == "focus" for command in commands))

    def test_compact_open_response_fetches_exact_context_tab(self):
        class CompactRunner(AuthRunner):
            def __call__(self, command, **kwargs):
                result = super().__call__(command, **kwargs)
                if command[1:2] == ["open-context"]:
                    result.stdout = json.dumps({"tabs": 2, "active_tabid": 9})
                return result

        runner = CompactRunner()
        auth = VimbrowserAuthenticator(context="utmail-helper", runner=runner)
        session = auth.acquire(mailbox="student@example.edu", interactive=True)
        self.assertEqual(session.source, "vimbrowser-context:utmail-helper")
        self.assertEqual(session.access_token, runner.access)
        commands = [call[0][1:] for call in runner.calls]
        self.assertEqual(commands[-2:], [["close-tab", "9"], ["focus", "4"]])

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
        self.assertEqual(commands[-1], ["close-tab", "9"])
        self.assertFalse(any(command[0] == "focus" for command in commands))
        self.assertFalse(any(command[:1] == ["frame-tree"] for command in commands))

    def test_silent_renewal_stops_early_once_resting_on_weblogin(self):
        clock = FakeClock()
        runner = AuthRunner(page_url="https://idpz.utorauth.utoronto.ca/idp/profile/SAML2/Redirect/SSO?execution=e1s1")
        auth = VimbrowserAuthenticator(context="utmail-helper", runner=runner, clock=clock, sleeper=clock.sleep)
        with self.assertRaisesRegex(SessionRequiredError, "utmail login --persistent"):
            auth.acquire(mailbox="student@example.edu", interactive=False)
        commands = [call[0][1:] for call in runner.calls]
        self.assertEqual(commands[-1], ["close-tab", "9"])
        self.assertFalse(any(command[0] == "focus" for command in commands))
        self.assertLess(clock.value - 1000.0, SIGN_IN_SETTLE_POLLS)  # well inside the 75 s budget

    def test_termination_signal_still_closes_the_helper_tab(self):
        class TerminatedRunner(AuthRunner):
            def __call__(self, command, **kwargs):
                result = super().__call__(command, **kwargs)
                if sum(call[0][1:] == ["tabs", "--json"] for call in self.calls) == 3:
                    os.kill(os.getpid(), signal.SIGTERM)  # a caller's `timeout` firing mid-poll
                return result

        runner = TerminatedRunner(page_url="https://login.microsoftonline.com/common/oauth2/authorize")
        auth = VimbrowserAuthenticator(context="utmail-helper", runner=runner)
        previous = signal.getsignal(signal.SIGTERM)
        try:
            with self.assertRaises(SystemExit) as raised:
                auth.acquire(mailbox="student@example.edu", interactive=False)
        finally:
            signal.signal(signal.SIGTERM, previous)
        self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
        commands = [call[0][1:] for call in runner.calls]
        self.assertEqual(commands[-1], ["close-tab", "9"])
        self.assertFalse(any(command[0] == "focus" for command in commands))

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

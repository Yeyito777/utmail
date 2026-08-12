from __future__ import annotations

import json
import subprocess
import unittest

from helpers import token
from utmail_tool.errors import BrowserImportError
from utmail_tool.vimbrowser import CAPTURE_JS, READ_CAPTURE_JS, TRIGGER_JS, VimbrowserImporter


class FakeRunner:
    def __init__(self, tabs: list[dict]):
        self.tabs = tabs
        self.calls: list[tuple[list[str], str | None]] = []
        self.raw_token = token(marker="browser-secret")

    def __call__(self, command, *, input, text, capture_output, timeout, check):
        self.calls.append((command, input))
        args = command[1:]
        if args == ["tabs", "--json"]:
            output = json.dumps({"active_tabid": 104, "tabs": self.tabs})
        elif args[:1] == ["focus"] or args[:1] in (["load"], ["reload"]):
            output = "{}"
        elif args[:2] == ["frame-tree", "5"]:
            output = json.dumps({"main_frame_id": "frame-1"})
        elif args[:3] == ["frame-js", "5", "frame-1"]:
            if input == READ_CAPTURE_JS:
                output = json.dumps({"ok": True, "type": "string", "result": "Bearer " + self.raw_token})
            elif input == TRIGGER_JS:
                output = json.dumps({"ok": True, "type": "string", "result": json.dumps({"ok": True})})
            elif input == CAPTURE_JS:
                output = json.dumps({"ok": True, "type": "string", "result": json.dumps({"ok": True})})
            else:
                raise AssertionError("unexpected frame script")
        else:
            raise AssertionError(f"unexpected command: {args}")
        return subprocess.CompletedProcess(command, 0, output, "")


class BrowserImportTests(unittest.TestCase):
    def test_exact_tab_import_restores_route_and_focus_without_token_in_argv(self):
        runner = FakeRunner([
            {"id": 5, "url": "https://outlook.cloud.microsoft/mail/inbox", "active": False},
            {"id": 104, "url": "https://example.com/", "active": True},
        ])
        importer = VimbrowserImporter(executable="vimbrowser-cli", runner=runner, sleeper=lambda _: None)
        raw = importer.import_bearer(tab_id=5)
        self.assertEqual(raw, runner.raw_token)
        flattened = [call[0][1:] for call in runner.calls]
        self.assertIn(["focus", "5"], flattened)
        self.assertIn(["load", "5", "https://outlook.cloud.microsoft/mail/inbox"], flattened)
        self.assertEqual(flattened[-1], ["focus", "104"])
        self.assertFalse(any(runner.raw_token in " ".join(command) for command, _ in runner.calls))

    def test_multiple_outlook_tabs_require_explicit_id(self):
        runner = FakeRunner([
            {"id": 5, "url": "https://outlook.cloud.microsoft/mail/", "active": False},
            {"id": 6, "url": "https://outlook.cloud.microsoft/mail/inbox", "active": False},
        ])
        importer = VimbrowserImporter(runner=runner, sleeper=lambda _: None)
        with self.assertRaises(BrowserImportError):
            importer.import_bearer()

    def test_malformed_or_lookalike_outlook_urls_are_not_candidates(self):
        runner = FakeRunner([
            {"id": 5, "url": "https://outlook.cloud.microsoft:invalid/mail/", "active": False},
            {"id": 6, "url": "https://outlook.cloud.microsoft.evil.example/mail/", "active": False},
        ])
        importer = VimbrowserImporter(runner=runner, sleeper=lambda _: None)
        with self.assertRaises(BrowserImportError):
            importer.import_bearer()

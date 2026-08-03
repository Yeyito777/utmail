from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

from .errors import BrowserImportError
from .paths import vimbrowser_cli


OUTLOOK_HOST = "outlook.cloud.microsoft"
CAPTURE_JS = r"""
(() => {
  window.__utmailImportedBearer = null;
  if (!window.__utmailOriginalFetch) {
    window.__utmailOriginalFetch = window.fetch;
    window.fetch = function(input, init) {
      const url = typeof input === 'string' ? input : input?.url;
      if (/\/owa\/service\.svc\b/i.test(String(url || ''))) {
        try {
          const headers = new Headers(init?.headers || input?.headers || {});
          const authorization = headers.get('authorization');
          if (authorization && /^Bearer\s+\S+$/i.test(authorization)) {
            window.__utmailImportedBearer = authorization;
          }
        } catch {}
      }
      return window.__utmailOriginalFetch.apply(this, arguments);
    };
  }
  return JSON.stringify({ok: true});
})()
""".strip()


TRIGGER_JS = r"""
(() => {
  // Inbox and Archive can each appear twice (Favorites and the mailbox tree).
  // Use a normally unique, non-mutating folder target and fail rather than
  // selecting the first match when Outlook's structure differs.
  const labels = ['Junk Email', 'Notes', 'Conversation History'];
  for (const target of labels) {
    const candidates = [...document.querySelectorAll('span')].filter(element =>
      (element.innerText || '').trim() === target &&
      element.parentElement?.getAttribute('role') === 'treeitem' &&
      element.parentElement.getClientRects().length > 0 &&
      !/selected/i.test(element.parentElement.innerText || '')
    );
    if (candidates.length === 1) {
      candidates[0].parentElement.click();
      return JSON.stringify({ok: true, target});
    }
  }
  return JSON.stringify({ok: false, target: null, count: 0});
})()
""".strip()


READ_CAPTURE_JS = "window.__utmailImportedBearer || ''"


@dataclass(frozen=True)
class BrowserTab:
    id: int
    url: str
    active: bool


class VimbrowserImporter:
    def __init__(
        self,
        *,
        executable: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.executable = executable or vimbrowser_cli()
        self.runner = runner
        self.sleeper = sleeper

    def _run(self, *arguments: str, stdin: str | None = None, timeout: float = 20) -> str:
        command = [self.executable, *arguments]
        try:
            completed = self.runner(
                command,
                input=stdin,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            raise BrowserImportError("vimbrowser-cli is not installed or discoverable") from None
        except subprocess.TimeoutExpired:
            raise BrowserImportError(f"vimbrowser command timed out: {arguments[0]}") from None
        if completed.returncode != 0:
            # Never echo command output: frame-js may legitimately contain a bearer.
            raise BrowserImportError(
                f"vimbrowser command failed: {arguments[0]} (exit {completed.returncode})"
            )
        return completed.stdout

    @staticmethod
    def _json(raw: str, operation: str) -> dict[str, Any]:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            raise BrowserImportError(f"vimbrowser returned invalid JSON for {operation}") from None
        if not isinstance(value, dict):
            raise BrowserImportError(f"vimbrowser returned an invalid result for {operation}")
        return value

    def tabs(self) -> tuple[int | None, list[BrowserTab]]:
        payload = self._json(self._run("tabs", "--json"), "tabs")
        rows = payload.get("tabs", payload if isinstance(payload, list) else None)
        if not isinstance(rows, list):
            raise BrowserImportError("vimbrowser did not return its tab list")
        active = payload.get("active_tabid")
        result: list[BrowserTab] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or "")
            try:
                parsed = urlsplit(url)
            except ValueError:
                continue
            if parsed.scheme == "https" and parsed.hostname == OUTLOOK_HOST:
                result.append(
                    BrowserTab(
                        id=int(row["id"]),
                        url=url,
                        active=bool(row.get("active")),
                    )
                )
        return int(active) if isinstance(active, int) else None, result

    def choose_tab(self, requested: int | None) -> tuple[int | None, BrowserTab]:
        active_id, tabs = self.tabs()
        if requested is not None:
            matches = [tab for tab in tabs if tab.id == requested]
            if len(matches) != 1:
                raise BrowserImportError(
                    f"tab {requested} is not an Outlook Web tab; choose one shown by `vimbrowser-cli tabs`"
                )
            return active_id, matches[0]
        active_matches = [tab for tab in tabs if tab.active]
        if len(active_matches) == 1:
            return active_id, active_matches[0]
        if len(tabs) == 1:
            return active_id, tabs[0]
        ids = ", ".join(str(tab.id) for tab in tabs) or "none"
        raise BrowserImportError(
            f"multiple or no Outlook Web tabs are available ({ids}); pass `--tab TAB_ID` explicitly"
        )

    def _frame_id(self, tab_id: int) -> str:
        payload = self._json(self._run("frame-tree", str(tab_id)), "frame-tree")
        frame_id = payload.get("main_frame_id")
        if not isinstance(frame_id, str) or not frame_id:
            raise BrowserImportError("Outlook Web has no current main frame")
        return frame_id

    def _frame_js(self, tab_id: int, frame_id: str, script: str) -> dict[str, Any]:
        return self._json(
            self._run("frame-js", str(tab_id), frame_id, stdin=script),
            "frame-js",
        )

    def import_bearer(self, *, tab_id: int | None = None) -> str:
        original_active, tab = self.choose_tab(tab_id)
        focused = False
        frame_id: str | None = None
        token: str | None = None
        try:
            self._run("focus", str(tab.id))
            focused = True
            self.sleeper(2.0)
            frame_id = self._frame_id(tab.id)
            capture = self._frame_js(tab.id, frame_id, CAPTURE_JS)
            if capture.get("ok") is False:
                raise BrowserImportError("could not install temporary read-only OWA token capture")
            trigger = self._frame_js(tab.id, frame_id, TRIGGER_JS)
            trigger_result = trigger.get("result", "")
            if isinstance(trigger_result, str):
                try:
                    decoded = json.loads(trigger_result)
                except json.JSONDecodeError:
                    decoded = {}
                if isinstance(decoded, dict) and decoded.get("ok") is False:
                    raise BrowserImportError(
                        "could not find one exact read-only folder navigation target in Outlook"
                    )
            for _ in range(20):
                self.sleeper(0.5)
                result = self._frame_js(tab.id, frame_id, READ_CAPTURE_JS)
                value = result.get("result")
                if isinstance(value, str) and value.startswith("Bearer ") and len(value) > 100:
                    token = value[7:]
                    break
            if not token:
                raise BrowserImportError(
                    "Outlook did not issue a readable mailbox request; reload Outlook and retry the explicit login"
                )
            return token
        finally:
            if focused:
                # Loading the original URL removes temporary page instrumentation and
                # restores the user's folder/message route without mutating mail.
                try:
                    self._run("load", str(tab.id), tab.url)
                    self.sleeper(1.0)
                except BrowserImportError:
                    try:
                        self._run("reload", str(tab.id))
                    except BrowserImportError:
                        pass
            if original_active is not None and original_active != tab.id:
                try:
                    self._run("focus", str(original_active))
                except BrowserImportError:
                    pass

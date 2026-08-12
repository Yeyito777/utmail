from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

from .errors import BrowserImportError, SessionRequiredError
from .paths import vimbrowser_cli, vimbrowser_context
from .session import DEFAULT_MAILBOX, OWA_APP_ID, OwaSession


OUTLOOK_HOST = "outlook.cloud.microsoft"
OUTLOOK_URL = "https://outlook.cloud.microsoft/mail/"
MAX_CLI_OUTPUT_BYTES = 512 * 1024
COMMAND_TIMEOUT_SECONDS = 20.0
POLL_SECONDS = 0.5
CONTEXT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


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


TOKEN_DISCOVERY_JS = rf"""
(() => {{
  const fail = reason => JSON.stringify({{ok: false, reason}});
  if (location.protocol !== 'https:' || location.hostname !== '{OUTLOOK_HOST}' ||
      location.port ||
      !(location.pathname === '/mail' || location.pathname.startsWith('/mail/'))) {{
    return fail('origin');
  }}
  if (localStorage.length > 5000) return fail('storage-size');
  const values = [];
  for (let index = 0; index < localStorage.length; index++) {{
    const key = localStorage.key(index);
    try {{
      const raw = localStorage.getItem(key) || '';
      if (raw.length > 262144) continue;
      const value = JSON.parse(raw);
      if (value && typeof value === 'object') values.push(value);
    }} catch {{}}
  }}
  const now = Math.floor(Date.now() / 1000);
  const common = value =>
    value.clientId === '{OWA_APP_ID}' &&
    typeof value.homeAccountId === 'string' && value.homeAccountId.length > 2 &&
    typeof value.secret === 'string' &&
    value.secret.length >= 100 && value.secret.length <= 131072 &&
    !/\s/.test(value.secret);
  const access = values.filter(value =>
    common(value) &&
    String(value.credentialType || '').toLowerCase() === 'accesstoken' &&
    /(?:^|\s)https:\/\/outlook\.office\.com\/OWA\.AccessAsUser\.All(?:\s|$)/i
      .test(String(value.target || '')) &&
    Number(value.expiresOn || 0) > now + 120
  );
  const refresh = values.filter(value =>
    common(value) &&
    String(value.credentialType || '').toLowerCase() === 'refreshtoken' &&
    Number(value.expiresOn || 0) > now + 120
  );
  const accountIds = [...new Set(access.map(value => value.homeAccountId))];
  if (accountIds.length !== 1) return fail('account-count');
  const accountId = accountIds[0];
  const accountAccess = access.filter(value => value.homeAccountId === accountId)
    .sort((left, right) => Number(right.expiresOn || 0) - Number(left.expiresOn || 0));
  const accountRefresh = refresh.filter(value => value.homeAccountId === accountId);
  if (accountAccess.length < 1 || accountRefresh.length !== 1) return fail('credential-count');
  return JSON.stringify({{
    ok: true,
    accessToken: accountAccess[0].secret,
    refreshToken: accountRefresh[0].secret,
    refreshTokenExpiresAt: Number(accountRefresh[0].expiresOn || 0),
    clientId: accountRefresh[0].clientId,
    accountId
  }});
}})()
""".strip()


@dataclass(frozen=True)
class BrowserTab:
    id: int
    url: str
    active: bool
    context: str | None = None


class VimbrowserDelegate:
    """Bounded, exact-tab vimbrowser-cli access shared by auth flows."""

    def __init__(
        self,
        *,
        executable: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.executable = executable or vimbrowser_cli()
        self.runner = runner
        self.clock = clock
        self.sleeper = sleeper

    def _run(self, *arguments: str, stdin: str | None = None, timeout: float = COMMAND_TIMEOUT_SECONDS) -> str:
        command = [self.executable, *arguments]
        try:
            completed = self.runner(
                command,
                input=stdin,
                text=True,
                capture_output=True,
                timeout=max(0.1, min(timeout, COMMAND_TIMEOUT_SECONDS)),
                check=False,
            )
        except FileNotFoundError:
            raise BrowserImportError("vimbrowser-cli is not installed or discoverable") from None
        except subprocess.TimeoutExpired:
            raise BrowserImportError(f"vimbrowser command timed out: {arguments[0]}") from None
        if completed.returncode != 0:
            # Never echo stdout/stderr: frame-js may legitimately contain credentials.
            raise BrowserImportError(
                f"vimbrowser command failed: {arguments[0]} (exit {completed.returncode})"
            )
        if not isinstance(completed.stdout, str):
            raise BrowserImportError(f"vimbrowser returned invalid output for {arguments[0]}")
        if len(completed.stdout.encode("utf-8")) > MAX_CLI_OUTPUT_BYTES:
            raise BrowserImportError(f"vimbrowser output exceeded the safety limit for {arguments[0]}")
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

    @staticmethod
    def _tab_id(value: Any, operation: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise BrowserImportError(f"vimbrowser returned no exact tab ID for {operation}")
        return value

    def tabs(self, *, timeout: float = COMMAND_TIMEOUT_SECONDS) -> tuple[int | None, list[BrowserTab]]:
        payload = self._json(self._run("tabs", "--json", timeout=timeout), "tabs")
        rows = payload.get("tabs")
        if not isinstance(rows, list):
            raise BrowserImportError("vimbrowser did not return its tab list")
        active_raw = payload.get("active_tabid")
        active = None if active_raw is None else self._tab_id(active_raw, "tabs")
        result: list[BrowserTab] = []
        seen: set[int] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                tab_id = self._tab_id(row.get("id"), "tabs")
            except BrowserImportError:
                continue
            if tab_id in seen:
                raise BrowserImportError("vimbrowser returned duplicate tab IDs")
            seen.add(tab_id)
            context_raw = row.get("context", row.get("context_name"))
            context = context_raw if isinstance(context_raw, str) and context_raw else None
            result.append(
                BrowserTab(
                    id=tab_id,
                    url=str(row.get("url") or ""),
                    active=tab_id == active or bool(row.get("active")),
                    context=context,
                )
            )
        return active, result

    @staticmethod
    def _is_outlook_url(url: str) -> bool:
        try:
            parsed = urlsplit(url)
            return (
                parsed.scheme == "https"
                and parsed.hostname == OUTLOOK_HOST
                and parsed.port is None
                and parsed.username is None
                and parsed.password is None
                and (parsed.path == "/mail" or parsed.path.startswith("/mail/"))
            )
        except ValueError:
            return False

    def _frame_id(self, tab_id: int, *, timeout: float = COMMAND_TIMEOUT_SECONDS) -> str:
        payload = self._json(
            self._run("frame-tree", str(tab_id), timeout=timeout),
            "frame-tree",
        )
        returned_tab = payload.get("tabid")
        if returned_tab is not None and self._tab_id(returned_tab, "frame-tree") != tab_id:
            raise BrowserImportError("vimbrowser returned a frame tree for a different tab")
        frame_id = payload.get("main_frame_id")
        if not isinstance(frame_id, str) or not frame_id:
            raise BrowserImportError("Outlook Web has no current main frame")
        return frame_id

    def _frame_js(
        self,
        tab_id: int,
        frame_id: str,
        script: str,
        *,
        timeout: float = COMMAND_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        payload = self._json(
            self._run("frame-js", str(tab_id), frame_id, stdin=script, timeout=timeout),
            "frame-js",
        )
        returned_tab = payload.get("tabid")
        if returned_tab is not None and self._tab_id(returned_tab, "frame-js") != tab_id:
            raise BrowserImportError("vimbrowser evaluated JavaScript in a different tab")
        return payload

    @staticmethod
    def _js_value(payload: dict[str, Any]) -> Any:
        if payload.get("ok") is False:
            raise BrowserImportError("vimbrowser could not evaluate the credential-safe Outlook probe")
        return payload.get("result", payload.get("value"))

    @classmethod
    def _js_object(cls, payload: dict[str, Any]) -> dict[str, Any]:
        value = cls._js_value(payload)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                raise BrowserImportError("Outlook returned an invalid credential probe result") from None
        if not isinstance(value, dict):
            raise BrowserImportError("Outlook returned an invalid credential probe result")
        return value


class VimbrowserImporter(VimbrowserDelegate):
    """Compatibility capture of one short-lived bearer from an existing exact tab."""

    def choose_tab(self, requested: int | None) -> tuple[int | None, BrowserTab]:
        active_id, all_tabs = self.tabs()
        tabs = [tab for tab in all_tabs if self._is_outlook_url(tab.url)]
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
            if self._js_object(capture).get("ok") is False:
                raise BrowserImportError("could not install temporary read-only OWA token capture")
            trigger = self._js_object(self._frame_js(tab.id, frame_id, TRIGGER_JS))
            if trigger.get("ok") is False:
                raise BrowserImportError(
                    "could not find one exact read-only folder navigation target in Outlook"
                )
            for _ in range(20):
                self.sleeper(POLL_SECONDS)
                value = self._js_value(self._frame_js(tab.id, frame_id, READ_CAPTURE_JS))
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


class VimbrowserAuthenticator(VimbrowserDelegate):
    """Acquire renewable OWA credentials through one isolated named context."""

    def __init__(self, *, context: str | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.context = vimbrowser_context() if context is None else context
        if not isinstance(self.context, str) or not CONTEXT_PATTERN.fullmatch(self.context):
            raise BrowserImportError(
                "UTMAIL_VIMBROWSER_CONTEXT must be 1-48 lowercase letters, numbers, '_' or '-'"
            )

    def _remaining(self, deadline: float) -> float:
        return max(0.1, min(COMMAND_TIMEOUT_SECONDS, deadline - self.clock()))

    def acquire(
        self,
        *,
        mailbox: str = DEFAULT_MAILBOX,
        interactive: bool,
        timeout_seconds: float | None = None,
    ) -> OwaSession:
        timeout = timeout_seconds if timeout_seconds is not None else (600.0 if interactive else 75.0)
        if timeout <= 0 or timeout > 1800:
            raise BrowserImportError("the vimbrowser authentication timeout is outside the safety bounds")
        deadline = self.clock() + timeout
        original_active, before_tabs = self.tabs(timeout=self._remaining(deadline))
        before_ids = {tab.id for tab in before_tabs}
        helper_tab_id: int | None = None
        try:
            opened = self._json(
                self._run(
                    "open-context",
                    self.context,
                    OUTLOOK_URL,
                    timeout=self._remaining(deadline),
                ),
                "open-context",
            )
            candidate_id = self._tab_id(opened.get("active_tabid"), "open-context")
            if candidate_id in before_ids:
                raise BrowserImportError("vimbrowser did not identify a new helper-opened tab")
            returned_context = opened.get("context", opened.get("context_name"))
            if (
                returned_context != self.context
                or not isinstance(opened.get("url"), str)
                or not self._is_outlook_url(opened["url"])
            ):
                # Do not close an ID unless vimbrowser confirms that it belongs to
                # the exact context and origin this helper just requested.
                raise BrowserImportError(
                    "vimbrowser did not confirm the exact newly opened Outlook context tab"
                )
            helper_tab_id = candidate_id

            while self.clock() < deadline:
                _, tabs = self.tabs(timeout=self._remaining(deadline))
                matches = [tab for tab in tabs if tab.id == helper_tab_id]
                if len(matches) != 1:
                    raise BrowserImportError("the exact helper-opened Outlook tab is no longer available")
                tab = matches[0]
                if tab.context != self.context:
                    raise BrowserImportError("the helper-opened tab changed browser context")
                if self._is_outlook_url(tab.url):
                    try:
                        frame_id = self._frame_id(helper_tab_id, timeout=self._remaining(deadline))
                        result = self._js_object(
                            self._frame_js(
                                helper_tab_id,
                                frame_id,
                                TOKEN_DISCOVERY_JS,
                                timeout=self._remaining(deadline),
                            )
                        )
                    except BrowserImportError:
                        result = {"ok": False}
                    if result.get("ok") is True and result.get("clientId") == OWA_APP_ID:
                        try:
                            refresh_expiry = int(result.get("refreshTokenExpiresAt") or 0)
                        except (TypeError, ValueError):
                            raise BrowserImportError(
                                "Outlook returned invalid renewable credential metadata"
                            ) from None
                        session = OwaSession.from_vimbrowser_tokens(
                            str(result.get("accessToken") or ""),
                            str(result.get("refreshToken") or ""),
                            refresh_token_expires_at=refresh_expiry,
                            mailbox=mailbox,
                            source=f"vimbrowser-context:{self.context}",
                        )
                        account_id = result.get("accountId")
                        expected_account_id = f"{session.object_id}.{session.tenant_id}"
                        if (
                            not isinstance(account_id, str)
                            or account_id.casefold() != expected_account_id.casefold()
                        ):
                            raise BrowserImportError(
                                "Outlook's renewable credential belongs to a different cached account"
                            )
                        return session
                self.sleeper(min(POLL_SECONDS, max(0.0, deadline - self.clock())))
        finally:
            if helper_tab_id is not None:
                try:
                    self._run("close-tab", str(helper_tab_id))
                except BrowserImportError:
                    pass
            if original_active is not None and original_active != helper_tab_id:
                try:
                    self._run("focus", str(original_active))
                except BrowserImportError:
                    pass

        if interactive:
            raise SessionRequiredError(
                "persistent login did not finish; rerun `utmail login --persistent` and complete Microsoft/U of T sign-in"
            )
        raise SessionRequiredError(
            "the named vimbrowser Outlook context needs human reauthentication; run `utmail login --persistent`"
        )

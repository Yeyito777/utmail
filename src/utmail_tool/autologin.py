"""Unattended U of T re-login when the persistent session has expired.

Approved by the owner (Oct 13 2026). When silent renewal needs a human, the
renewal path reopens the usual persistent-login tab with an ``Assistant`` that
fills U of T weblogin with ``UOFT_UTORID``/``UOFT_UTORID_PASSWORD`` (read through
``secrets run``; never printed, logged, or put in argv), reads the Verified Push
code from that same Duo page, and asks ``duo own-login approve`` to approve only
the matching request. duo-cli enforces the lock, daily cap, request matching and
audit log (see its SECURITY.md). Any failure keeps the original
SessionRequiredError, with the reason appended.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar
from urllib.parse import urlsplit

from .errors import SessionRequiredError, UtmailError
from .paths import state_dir, vimbrowser_cli
from .storage import atomic_write_json, read_private_json


TOOL = "utmail"
DISABLE_ENV = "UTMAIL_AUTOLOGIN"
WEBLOGIN_HOST = "idpz.utorauth.utoronto.ca"
DUO_HOST_RE = re.compile(r"^api-[0-9a-f]{8}\.duosecurity\.com$")
DUO_DEVICE = "Exocortex local"
LOGIN_TIMEOUT_SECONDS = 150.0
MIN_INTERVAL_SECONDS = 30 * 60
COMMAND_TIMEOUT_SECONDS = 30.0
APPROVE_TIMEOUT_SECONDS = 60.0

# Runs inside `secrets run`; the values reach this process only through a pipe.
READ_CREDENTIALS_PY = (
    "import json, os, sys; "
    "json.dump([os.environ['UOFT_UTORID'], os.environ['UOFT_UTORID_PASSWORD']], sys.stdout)"
)

# `pending` marks the document we already submitted: until its POST navigates
# away, the old form is still on screen and must not read as a rejection.
WEBLOGIN_PROBE_JS = r"""
(() => JSON.stringify({form: location.origin === 'https://idpz.utorauth.utoronto.ca' &&
  !!document.getElementById('username') && !!document.getElementById('password') &&
  !!document.getElementById('login-btn'),
  pending: document.documentElement.hasAttribute('data-exo-submitted')}))()
""".strip()

# __UTORID__/__PASSWORD__ are replaced with JSON string literals; the script is
# delivered to vimbrowser on stdin and its result contains no credential.
WEBLOGIN_FILL_JS = r"""
(() => {
  if (location.origin !== 'https://idpz.utorauth.utoronto.ca') return JSON.stringify({ok: false});
  const u = document.getElementById('username'), p = document.getElementById('password');
  const b = document.getElementById('login-btn');
  if (!u || !p || !b) return JSON.stringify({ok: false});
  const set = (el, v) => { el.focus(); el.value = v;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true})); };
  set(u, __UTORID__); set(p, __PASSWORD__);
  document.documentElement.setAttribute('data-exo-submitted', '');
  setTimeout(() => b.click(), 50);
  return JSON.stringify({ok: true});
})()
""".strip()

DUO_PROBE_JS = r"""
(() => {
  if (location.protocol !== 'https:' || !/^api-[0-9a-f]{8}\.duosecurity\.com$/.test(location.hostname))
    return JSON.stringify({ok: false});
  const lines = (document.body ? document.body.innerText : '').split('\n').map(s => s.trim()).filter(Boolean);
  const trust = document.getElementById('trust-browser-button');
  return JSON.stringify({
    ok: true,
    codes: [...new Set(lines.filter(line => /^[0-9]{3,8}$/.test(line)))],
    sentTo: lines.find(line => line.startsWith('Sent to ')) || '',
    trust: !!(trust && trust.getClientRects().length)
  });
})()
""".strip()

# Exact, visible, single-target clicks only; __ACTION__ and __DEVICE__ are
# replaced with JSON string literals.
DUO_ACTION_JS = r"""
(() => {
  const action = __ACTION__, device = __DEVICE__;
  const visible = el => el.getClientRects().length > 0;
  const text = el => (el.innerText || el.textContent || '').trim();
  const one = list => list.length === 1 ? list[0] : null;
  let target = null;
  if (action === 'remember') {
    target = one([...document.querySelectorAll('input[type=checkbox]')].filter(box =>
      !box.checked && /Remember me/i.test(text(box.closest('label') || box.parentElement || box))));
  } else if (action === 'trust') {
    target = document.getElementById('trust-browser-button');
  } else if (action === 'other-options') {
    target = one([...document.querySelectorAll('a, button')].filter(el => visible(el) && text(el) === 'Other options'));
  } else if (action === 'pick-device') {
    target = one([...document.querySelectorAll('a, button, [role=button], [role=link]')].filter(el =>
      visible(el) && text(el).includes('Duo Push') && text(el).includes(device)));
  }
  if (!target) return JSON.stringify({ok: false});
  target.click();
  return JSON.stringify({ok: true});
})()
""".strip()


Runner = Callable[..., subprocess.CompletedProcess[str]]
T = TypeVar("T")


def is_sign_in_url(url: str) -> bool:
    """True for U of T weblogin and Duo pages, where only a human (or Assistant) can proceed."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = parts.hostname or ""
    return parts.scheme == "https" and port in (None, 443) and (host == WEBLOGIN_HOST or bool(DUO_HOST_RE.fullmatch(host)))


@contextmanager
def terminable() -> Iterator[None]:
    """Turn SIGTERM/SIGHUP into SystemExit so ``finally`` cleanup still runs.

    Callers often wrap commands in ``timeout``. Python's default SIGTERM kills
    the process outright, leaving the helper tab open and the own-login ticket
    unreleased. The first signal raises; later ones are ignored so cleanup
    finishes. Outside the main thread this is a no-op.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous: dict[int, Any] = {}
    fired = False

    def handler(signum: int, _frame: Any) -> None:
        nonlocal fired
        fired = True
        for number in previous:
            signal.signal(number, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    for number in (signal.SIGTERM, signal.SIGHUP):
        previous[number] = signal.signal(number, handler)
    try:
        yield
    finally:
        if not fired:
            for number, old in previous.items():
                signal.signal(number, signal.SIG_DFL if old is None else old)


class _Transient(Exception):
    """A browser probe failed mid-navigation; try again on the next poll."""


class Assistant:
    """Drives one helper tab through U of T weblogin and Duo, at most once each."""

    def __init__(
        self,
        ticket: str,
        utorid: str,
        password: str,
        *,
        runner: Runner = subprocess.run,
        vimbrowser: str | None = None,
        duo: str = "duo",
    ):
        self.ticket = ticket
        self.utorid = utorid
        self.password = password
        self.runner = runner
        self.vimbrowser = vimbrowser or vimbrowser_cli()
        self.duo = duo
        self.submitted = False
        self.approved = False
        self.trusted = False
        self.switch_stage = 0

    def _browser(self, *arguments: str, stdin: str | None = None) -> dict[str, Any]:
        try:
            completed = self.runner(
                [self.vimbrowser, *arguments],
                input=stdin,
                text=True,
                capture_output=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
                check=False,
            )
            value = json.loads(completed.stdout) if completed.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            value = None
        if not isinstance(value, dict):
            raise _Transient()
        return value

    def _js(self, tab_id: int, script: str) -> dict[str, Any]:
        frame = self._browser("frame-tree", str(tab_id)).get("main_frame_id")
        if not isinstance(frame, str) or not frame:
            raise _Transient()
        payload = self._browser("frame-js", str(tab_id), frame, stdin=script)
        value = payload.get("result", payload.get("value"))
        try:
            value = json.loads(value) if isinstance(value, str) else value
        except json.JSONDecodeError:
            value = None
        if payload.get("ok") is False or not isinstance(value, dict):
            raise _Transient()
        return value

    def _action(self, tab_id: int, action: str) -> bool:
        script = DUO_ACTION_JS.replace("__ACTION__", json.dumps(action)).replace("__DEVICE__", json.dumps(DUO_DEVICE))
        return self._js(tab_id, script).get("ok") is True

    def _tab_url(self, tab_id: int) -> str:
        rows = self._browser("tabs", "--json").get("tabs")
        urls = [str(row.get("url") or "") for row in rows or [] if isinstance(row, dict) and row.get("id") == tab_id]
        if len(urls) != 1:
            raise _Transient()
        return urls[0]

    def step(self, tab_id: int, url: str | None = None) -> None:
        """Advance the login by at most one action; called from the auth poll loop."""
        try:
            self._step(tab_id, url if url is not None else self._tab_url(tab_id))
        except _Transient:
            pass

    def _step(self, tab_id: int, url: str) -> None:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.scheme != "https" or parts.port not in (None, 443):
            return
        if host == WEBLOGIN_HOST:
            probe = self._js(tab_id, WEBLOGIN_PROBE_JS)
            if not probe.get("form") or probe.get("pending"):
                return
            if self.submitted:
                # Never retry a password: a second form means it was rejected.
                raise SessionRequiredError("U of T weblogin did not accept the stored credentials")
            self.submitted = True
            script = WEBLOGIN_FILL_JS.replace("__UTORID__", json.dumps(self.utorid)).replace(
                "__PASSWORD__", json.dumps(self.password)
            )
            self._js(tab_id, script)
            return
        if not DUO_HOST_RE.fullmatch(host):
            return
        page = self._js(tab_id, DUO_PROBE_JS)
        if self.approved:
            if page.get("trust") and not self.trusted:
                self.trusted = True
                self._action(tab_id, "trust")
            return
        if self.switch_stage == 1:
            if self._action(tab_id, "pick-device"):
                self.switch_stage = 2
            return
        codes, sent_to = page.get("codes"), str(page.get("sentTo") or "")
        if not isinstance(codes, list) or not codes or not sent_to:
            return
        if DUO_DEVICE not in sent_to:
            if self.switch_stage == 0 and self._action(tab_id, "other-options"):
                self.switch_stage = 1
            return
        if len(codes) != 1 or not isinstance(codes[0], str):
            raise SessionRequiredError("the Duo page showed an ambiguous Verified Push code")
        self._action(tab_id, "remember")
        self.approved = True
        self._approve(codes[0])

    def _approve(self, code: str) -> None:
        try:
            completed = self.runner(
                [self.duo, "own-login", "approve", self.ticket, "--json"],
                input=code,
                text=True,
                capture_output=True,
                timeout=APPROVE_TIMEOUT_SECONDS,
                check=False,
                env={**os.environ, "UOFT_UTORID": self.utorid},
            )
        except (OSError, subprocess.TimeoutExpired):
            raise SessionRequiredError("duo own-login approve could not run") from None
        if completed.returncode != 0:
            # duo-cli errors are sanitized and never contain the code.
            detail = (completed.stderr or "").strip().splitlines()
            raise SessionRequiredError(
                "Duo refused the automatic approval: " + (detail[0][:200] if detail else f"exit {completed.returncode}")
            )


def _marker_path() -> Path:
    return state_dir() / "autologin.json"


def _duo(runner: Runner, *arguments: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return runner(
            ["duo", "own-login", *arguments],
            text=True,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _read_credentials(runner: Runner) -> tuple[str, str]:
    try:
        completed = runner(
            [
                "secrets", "run",
                "--allow", "UOFT_UTORID",
                "--allow", "UOFT_UTORID_PASSWORD",
                "--purpose", f"{TOOL} automatic U of T re-login",
                "--", sys.executable, "-I", "-c", READ_CREDENTIALS_PY,
            ],
            text=True,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
        values = json.loads(completed.stdout) if completed.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        values = None
    if not (isinstance(values, list) and len(values) == 2 and all(isinstance(v, str) and v for v in values)):
        raise SessionRequiredError("could not read UOFT_UTORID/UOFT_UTORID_PASSWORD from secrets")
    return values[0], values[1]


def attempt(
    login: Callable[[Assistant], T],
    original: SessionRequiredError,
    *,
    runner: Runner = subprocess.run,
    clock: Callable[[], float] = time.time,
    marker: Path | None = None,
) -> T:
    """Try one rate-limited unattended login; return its result or raise ``original``."""

    def give_up(reason: str) -> SessionRequiredError:
        return SessionRequiredError(f"{original.message}; automatic re-login {reason}")

    if os.environ.get(DISABLE_ENV, "1") == "0":
        raise original
    target = marker or _marker_path()
    try:
        last = float(read_private_json(target).get("lastAttemptAt", 0))
    except (FileNotFoundError, UtmailError, AttributeError, TypeError, ValueError):
        last = 0.0
    if clock() - last < MIN_INTERVAL_SECONDS:
        raise give_up("skipped (last attempt under 30 minutes ago)")
    atomic_write_json(target, {"lastAttemptAt": clock()})
    with terminable():
        return _attempt(login, runner, give_up)


def _attempt(login: Callable[[Assistant], T], runner: Runner, give_up: Callable[[str], SessionRequiredError]) -> T:
    begun = _duo(runner, "begin", "--caller", TOOL, "--json")
    try:
        ticket = json.loads(begun.stdout)["ownLogin"]["ticket"] if begun and begun.returncode == 0 else None
    except (json.JSONDecodeError, KeyError, TypeError):
        ticket = None
    if not isinstance(ticket, str):
        detail = (begun.stderr or "").strip().splitlines() if begun else []
        raise give_up("not started: " + (detail[0][:200] if detail else "duo own-login begin failed"))

    outcome = "failed"
    try:
        utorid, password = _read_credentials(runner)
        result = login(Assistant(ticket, utorid, password, runner=runner))
        outcome = "completed"
        return result
    except UtmailError as error:
        raise give_up(f"failed: {error.message}") from None
    finally:
        _duo(runner, "end", ticket, "--outcome", outcome)

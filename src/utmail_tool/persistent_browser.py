from __future__ import annotations

import shutil
import stat
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .errors import SessionRequiredError, UnsafeFileError
from .paths import browser_profile_path
from .session import DEFAULT_MAILBOX, OWA_APP_ID, OwaSession
from .storage import ensure_private_directory


OUTLOOK_URL = "https://outlook.cloud.microsoft/mail/"
TOKEN_DISCOVERY_JS = r"""
() => {
  const values = Object.keys(localStorage).map(key => {
    try { return JSON.parse(localStorage.getItem(key) || ''); }
    catch { return null; }
  }).filter(Boolean);
  const refresh = values.filter(value =>
    String(value.credentialType || '').toLowerCase() === 'refreshtoken' &&
    value.clientId === '9199bf20-a13f-4107-85dc-02114787ef48' &&
    typeof value.secret === 'string'
  );
  const now = Math.floor(Date.now() / 1000);
  const access = values.filter(value =>
    String(value.credentialType || '').toLowerCase() === 'accesstoken' &&
    value.clientId === '9199bf20-a13f-4107-85dc-02114787ef48' &&
    String(value.target || '').includes('OWA.AccessAsUser.All') &&
    typeof value.secret === 'string' &&
    Number(value.expiresOn || 0) > now + 120
  ).sort((left, right) => Number(right.expiresOn || 0) - Number(left.expiresOn || 0));
  if (refresh.length !== 1 || access.length < 1) {
    return {ok: false, refreshCount: refresh.length, accessCount: access.length};
  }
  return {
    ok: true,
    accessToken: access[0].secret,
    refreshToken: refresh[0].secret,
    refreshTokenExpiresAt: Number(refresh[0].expiresOn || 0),
    clientId: refresh[0].clientId
  };
}
""".strip()


class PersistentBrowserAuthenticator:
    def __init__(
        self,
        *,
        profile: Path | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.profile = (profile or browser_profile_path()).expanduser()
        self.clock = clock
        self.sleeper = sleeper

    def _prepare_profile(self) -> None:
        ensure_private_directory(self.profile)
        self.profile.chmod(0o700)

    @staticmethod
    def _playwright():
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise SessionRequiredError(
                "persistent Outlook authentication support is not installed; run `uv sync` in the UTmail helper"
            ) from None
        return sync_playwright, PlaywrightError

    def acquire(
        self,
        *,
        mailbox: str = DEFAULT_MAILBOX,
        interactive: bool,
        timeout_seconds: float | None = None,
    ) -> OwaSession:
        if not interactive and not self.profile.exists():
            raise SessionRequiredError("the helper-owned Outlook browser session has not been initialized")
        self._prepare_profile()
        sync_playwright, playwright_error = self._playwright()
        timeout = timeout_seconds if timeout_seconds is not None else (600.0 if interactive else 75.0)
        deadline = self.clock() + timeout
        last_url = ""
        try:
            with sync_playwright() as engine:
                context = engine.chromium.launch_persistent_context(
                    str(self.profile),
                    headless=not interactive,
                    viewport={"width": 1280, "height": 900},
                )
                try:
                    pages = context.pages
                    page = pages[0] if pages else context.new_page()
                    try:
                        page.goto(OUTLOOK_URL, wait_until="domcontentloaded", timeout=30_000)
                    except playwright_error:
                        # Authentication redirects and slow Outlook startup can outlive
                        # DOMContentLoaded. Poll all live pages below instead of failing.
                        pass
                    while self.clock() < deadline:
                        for candidate in list(context.pages):
                            last_url = candidate.url or last_url
                            try:
                                parsed = urlsplit(candidate.url)
                            except ValueError:
                                continue
                            if parsed.scheme != "https" or parsed.hostname != "outlook.cloud.microsoft":
                                continue
                            try:
                                tokens: Any = candidate.evaluate(TOKEN_DISCOVERY_JS)
                            except playwright_error:
                                continue
                            if not isinstance(tokens, dict) or not tokens.get("ok"):
                                continue
                            if tokens.get("clientId") != OWA_APP_ID:
                                continue
                            return OwaSession.from_persistent_tokens(
                                str(tokens.get("accessToken") or ""),
                                str(tokens.get("refreshToken") or ""),
                                refresh_token_expires_at=int(tokens.get("refreshTokenExpiresAt") or 0),
                                mailbox=mailbox,
                            )
                        self.sleeper(0.5)
                finally:
                    context.close()
        except SessionRequiredError:
            raise
        except playwright_error:
            raise SessionRequiredError(
                "the helper-owned Outlook browser session could not start; rerun `utmail login --persistent` interactively"
            ) from None
        location = "Microsoft/U of T sign-in" if "login" in last_url or "utoronto" in last_url else "Outlook"
        if interactive:
            raise SessionRequiredError(
                f"persistent login did not finish at {location}; rerun `utmail login --persistent` and complete sign-in"
            )
        raise SessionRequiredError(
            "the helper-owned Outlook session needs human reauthentication; run `utmail login --persistent`"
        )


def delete_browser_profile(*, profile: Path | None = None) -> bool:
    target = (profile or browser_profile_path()).expanduser()
    try:
        info = target.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnsafeFileError("refused to delete a non-directory UTmail browser profile")
    shutil.rmtree(target)
    return True

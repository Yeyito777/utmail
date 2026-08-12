from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable

import requests

from .errors import NetworkError, SessionRejectedError, SessionRequiredError
from .paths import lock_path, session_path
from .session import EXPIRY_LEEWAY_SECONDS, OWA_APP_ID, OwaSession
from .storage import atomic_write_json, exclusive_lock, read_private_json
from .vimbrowser import VimbrowserAuthenticator


TOKEN_ORIGIN = "https://outlook.cloud.microsoft"
TOKEN_SCOPE = "https://outlook.office.com/.default"
MAX_TOKEN_RESPONSE_BYTES = 1024 * 1024


def refresh_direct(
    session: OwaSession,
    *,
    transport: requests.Session | None = None,
    now: Callable[[], float] = time.time,
) -> OwaSession:
    if not session.can_refresh_directly(now=now()):
        raise SessionRequiredError("the current OWA refresh window ended")
    client = transport or requests.Session()
    endpoint = f"https://login.microsoftonline.com/{session.tenant_id}/oauth2/v2.0/token"
    try:
        response = client.post(
            endpoint,
            headers={"Origin": TOKEN_ORIGIN, "Referer": TOKEN_ORIGIN + "/"},
            data={
                "client_id": OWA_APP_ID,
                "grant_type": "refresh_token",
                "refresh_token": session.refresh_token,
                "scope": TOKEN_SCOPE,
            },
            timeout=(10, 30),
            allow_redirects=False,
        )
    except requests.RequestException:
        raise NetworkError("could not reach Microsoft to renew the Outlook session") from None
    if 300 <= response.status_code < 400:
        response.close()
        raise NetworkError("refused an OAuth token-endpoint redirect")
    content = response.content
    response.close()
    if len(content) > MAX_TOKEN_RESPONSE_BYTES:
        raise NetworkError("Microsoft's token response exceeded the configured safety limit")
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, UnicodeError):
        raise NetworkError("Microsoft returned an unexpected token response") from None
    if not isinstance(payload, dict):
        raise NetworkError("Microsoft returned an invalid token response")
    if response.status_code != 200:
        code = str(payload.get("error") or "")
        if code in {"invalid_grant", "interaction_required", "login_required", "invalid_request"}:
            raise SessionRequiredError(
                "the OWA renewable credential needs a fresh browser authentication"
            )
        raise NetworkError(f"Microsoft token renewal failed with HTTP {response.status_code}")
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        raise NetworkError("Microsoft's token response omitted rotated credentials")
    return session.with_refreshed_tokens(access_token, refresh_token)


def _read_saved(path: Path) -> OwaSession:
    try:
        raw = read_private_json(path)
    except FileNotFoundError:
        raise SessionRequiredError("UTmail is not logged in; run `utmail login --persistent`") from None
    if not isinstance(raw, dict):
        raise SessionRejectedError("the saved UTmail session has an invalid format")
    return OwaSession.from_dict(raw)


def load_or_refresh_session(
    *,
    path: Path | None = None,
    lock: Path | None = None,
    direct_refresher: Callable[[OwaSession], OwaSession] = refresh_direct,
    browser_authenticator: VimbrowserAuthenticator | None = None,
) -> OwaSession:
    target = path or session_path()
    lock_target = lock or lock_path()
    session = _read_saved(target)
    try:
        session.assert_current()
        return session
    except SessionRequiredError:
        pass

    with exclusive_lock(lock_target):
        # A concurrent process may already have renewed and atomically replaced it.
        session = _read_saved(target)
        try:
            session.assert_current()
            return session
        except SessionRequiredError:
            pass

        renewed: OwaSession | None = None
        if session.can_refresh_directly():
            try:
                renewed = direct_refresher(session)
            except (SessionRequiredError, SessionRejectedError):
                renewed = None
        if renewed is None:
            if session.renewal_mode != "vimbrowser":
                raise SessionRequiredError(
                    "the imported OWA token expired; run `utmail login --persistent` for automatic renewal"
                )
            authenticator = browser_authenticator or VimbrowserAuthenticator()
            renewed = authenticator.acquire(mailbox=session.mailbox, interactive=False)
            if renewed.tenant_id != session.tenant_id or renewed.object_id != session.object_id:
                raise SessionRejectedError("the named vimbrowser context authenticated a different account")

        renewed.assert_current()
        atomic_write_json(target, asdict(renewed))
        return renewed

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import SessionRejectedError, SessionRequiredError
from .paths import lock_path, session_path
from .storage import atomic_write_json, exclusive_lock, read_private_json


OWA_APP_ID = "9199bf20-a13f-4107-85dc-02114787ef48"
OWA_AUDIENCE = "https://outlook.office.com"
OWA_BASE_URL = "https://outlook.cloud.microsoft"
DEFAULT_MAILBOX = os.environ.get("UTMAIL_MAILBOX", "").strip()
SESSION_VERSION = 2
EXPIRY_LEEWAY_SECONDS = 60
RENEWAL_MODES = {"none", "vimbrowser"}


def utc_iso(timestamp: int | float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _decode_segment(value: str) -> dict[str, Any]:
    try:
        value += "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(value.encode("ascii"))
        result = json.loads(decoded)
    except (ValueError, UnicodeError, json.JSONDecodeError):
        raise SessionRejectedError("the supplied OWA bearer is not a valid JWT") from None
    if not isinstance(result, dict):
        raise SessionRejectedError("the supplied OWA bearer has an invalid payload")
    return result


def normalize_token_input(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise SessionRejectedError("no OWA bearer was provided")
    if len(value) > 128 * 1024:
        raise SessionRejectedError("the supplied OWA bearer is unexpectedly large")
    if value.startswith("{"):
        try:
            parsed = json.loads(value)
            value = str(parsed["access_token"]).strip()
        except (json.JSONDecodeError, KeyError, TypeError):
            raise SessionRejectedError("session JSON must contain an access_token string") from None
    if value.lower().startswith("authorization:"):
        value = value.split(":", 1)[1].strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if any(character.isspace() for character in value):
        raise SessionRejectedError("the supplied OWA bearer contains whitespace")
    return value


def normalize_refresh_token(raw: str) -> str:
    value = str(raw).strip()
    if not 100 <= len(value) <= 128 * 1024 or any(character.isspace() for character in value):
        raise SessionRejectedError("the supplied OWA renewable credential has an invalid format")
    return value


def normalize_mailbox(raw: str) -> str:
    value = str(raw).strip()
    if not value or len(value) > 320 or value.count("@") != 1 or any(character.isspace() for character in value):
        raise SessionRejectedError(
            "a valid mailbox is required; pass `--mailbox ADDRESS` or set UTMAIL_MAILBOX"
        )
    return value


def validate_token(token: str, *, now: float | None = None, require_current: bool = True) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise SessionRejectedError("the supplied OWA bearer is not a three-part JWT")
    claims = _decode_segment(parts[1])
    audience = claims.get("aud")
    if audience != OWA_AUDIENCE:
        raise SessionRejectedError("the bearer was not issued for Outlook Web")
    app_id = claims.get("appid") or claims.get("azp")
    if app_id != OWA_APP_ID:
        raise SessionRejectedError("the bearer was not issued to the Outlook Web application")
    expires = claims.get("exp")
    if not isinstance(expires, int):
        raise SessionRejectedError("the bearer does not contain a valid expiry")
    current = time.time() if now is None else now
    if require_current and expires <= current + EXPIRY_LEEWAY_SECONDS:
        raise SessionRequiredError("the OWA access token has expired and could not yet be renewed")
    tenant = claims.get("tid")
    object_id = claims.get("oid")
    if not isinstance(tenant, str) or not tenant or not isinstance(object_id, str) or not object_id:
        raise SessionRejectedError("the bearer is missing its U of T account identity")
    scopes = set(str(claims.get("scp") or "").split())
    if not scopes.intersection({"Mail.Read", "Mail.ReadWrite", "OWA.AccessAsUser.All", "OutlookService.AccessAsUser.All"}):
        raise SessionRejectedError("the bearer does not include Outlook mailbox access")
    return claims


@dataclass(frozen=True)
class OwaSession:
    version: int
    access_token: str
    mailbox: str
    base_url: str
    imported_at: int
    expires_at: int
    tenant_id: str
    object_id: str
    app_id: str
    source: str
    refresh_token: str | None = None
    refresh_token_expires_at: int | None = None
    renewal_mode: str = "none"

    @classmethod
    def from_token(cls, raw: str, *, mailbox: str = DEFAULT_MAILBOX, source: str) -> "OwaSession":
        token = normalize_token_input(raw)
        claims = validate_token(token)
        return cls(
            version=SESSION_VERSION,
            access_token=token,
            mailbox=normalize_mailbox(mailbox),
            base_url=OWA_BASE_URL,
            imported_at=int(time.time()),
            expires_at=int(claims["exp"]),
            tenant_id=str(claims["tid"]),
            object_id=str(claims["oid"]),
            app_id=str(claims.get("appid") or claims.get("azp")),
            source=source,
        )

    @classmethod
    def from_vimbrowser_tokens(
        cls,
        access_token: str,
        refresh_token: str,
        *,
        refresh_token_expires_at: int,
        mailbox: str = DEFAULT_MAILBOX,
        source: str = "vimbrowser-context",
    ) -> "OwaSession":
        session = cls.from_token(access_token, mailbox=mailbox, source=source)
        renewable = normalize_refresh_token(refresh_token)
        expiry = int(refresh_token_expires_at)
        if expiry <= 0:
            raise SessionRejectedError("the captured OWA renewable credential has no valid expiry metadata")
        # Outlook may still hold a valid access token after the SPA
        # refresh token's hard 24-hour window. Preserve that access token as a
        # bridge; can_refresh_directly() will refuse the stale refresh token and
        # the named vimbrowser context can reauthorize when the access token ends.
        return replace(
            session,
            refresh_token=renewable,
            refresh_token_expires_at=expiry,
            renewal_mode="vimbrowser",
        )

    @classmethod
    def from_vimbrowser_bearer(
        cls,
        access_token: str,
        *,
        mailbox: str = DEFAULT_MAILBOX,
        source: str = "vimbrowser-context",
    ) -> "OwaSession":
        """Create a browser-renewable session from one captured OWA request.

        Current Outlook builds may keep their refresh-token cache in an opaque
        application-owned format.  The named vimbrowser context is still the
        durable credential owner in that case: once this short-lived bearer
        expires, renewal reopens that exact context and captures a fresh
        read-only mailbox request.
        """
        session = cls.from_token(access_token, mailbox=mailbox, source=source)
        return replace(session, renewal_mode="vimbrowser")

    @classmethod
    def from_persistent_tokens(
        cls,
        access_token: str,
        refresh_token: str,
        *,
        refresh_token_expires_at: int,
        mailbox: str = DEFAULT_MAILBOX,
        source: str = "vimbrowser-context",
    ) -> "OwaSession":
        """Compatibility constructor for callers using the pre-vimbrowser name."""
        return cls.from_vimbrowser_tokens(
            access_token,
            refresh_token,
            refresh_token_expires_at=refresh_token_expires_at,
            mailbox=mailbox,
            source=source,
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "OwaSession":
        data = dict(raw)
        if data.get("version") == 1:
            data.update(
                version=SESSION_VERSION,
                refresh_token=None,
                refresh_token_expires_at=None,
                renewal_mode="none",
            )
        if data.get("renewal_mode") == "persistent-browser":
            # Version-2 sessions from releases before 0.4 remain wire-compatible.
            # Only ownership/renewal terminology changes; credentials are untouched.
            data["renewal_mode"] = "vimbrowser"
            if data.get("source") == "persistent-browser":
                data["source"] = "vimbrowser-context"
        try:
            session = cls(**data)
        except (TypeError, KeyError, ValueError):
            raise SessionRejectedError("the saved UTmail session has an invalid format") from None
        if session.version != SESSION_VERSION or session.base_url != OWA_BASE_URL:
            raise SessionRejectedError("the saved UTmail session has an unsupported format")
        if session.renewal_mode not in RENEWAL_MODES:
            raise SessionRejectedError("the saved UTmail session has an invalid renewal mode")
        claims = validate_token(session.access_token, require_current=False)
        if (
            session.tenant_id != str(claims["tid"])
            or session.object_id != str(claims["oid"])
            or session.app_id != str(claims.get("appid") or claims.get("azp"))
            or session.expires_at != int(claims["exp"])
        ):
            raise SessionRejectedError("the saved UTmail session identity does not match its bearer")
        normalize_mailbox(session.mailbox)
        if session.refresh_token is not None:
            normalize_refresh_token(session.refresh_token)
        return session

    def assert_current(self, *, now: float | None = None) -> None:
        validate_token(self.access_token, now=now)

    def can_refresh_directly(self, *, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return bool(
            self.refresh_token
            and self.refresh_token_expires_at
            and self.refresh_token_expires_at > current + EXPIRY_LEEWAY_SECONDS
        )

    def with_refreshed_tokens(self, access_token: str, refresh_token: str) -> "OwaSession":
        token = normalize_token_input(access_token)
        claims = validate_token(token)
        if str(claims["tid"]) != self.tenant_id or str(claims["oid"]) != self.object_id:
            raise SessionRejectedError("the renewed OWA token belongs to a different account")
        return replace(
            self,
            access_token=token,
            refresh_token=normalize_refresh_token(refresh_token),
            imported_at=int(time.time()),
            expires_at=int(claims["exp"]),
            source=self.source,
        )

    def public(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "authenticated": True,
            "mailbox": self.mailbox,
            "source": self.source,
            "importedAt": utc_iso(self.imported_at),
            "expiresAt": utc_iso(self.expires_at),
            "secondsRemaining": max(0, self.expires_at - int(time.time())),
            "automaticRenewal": self.renewal_mode == "vimbrowser",
            "renewalMode": self.renewal_mode,
        }
        if self.refresh_token_expires_at:
            result["currentRefreshWindowEndsAt"] = utc_iso(self.refresh_token_expires_at)
        return result


def save_session(session: OwaSession, *, path: Path | None = None, lock: Path | None = None) -> None:
    target = path or session_path()
    lock_target = lock or lock_path()
    with exclusive_lock(lock_target):
        atomic_write_json(target, asdict(session))


def load_session(*, path: Path | None = None, require_current: bool = True) -> OwaSession:
    target = path or session_path()
    try:
        data = read_private_json(target)
    except FileNotFoundError:
        raise SessionRequiredError("UTmail is not logged in; run `utmail login --persistent`") from None
    if not isinstance(data, dict):
        raise SessionRejectedError("the saved UTmail session has an invalid format")
    session = OwaSession.from_dict(data)
    if require_current:
        session.assert_current()
    return session


def delete_session(*, path: Path | None = None, lock: Path | None = None) -> bool:
    target = path or session_path()
    lock_target = lock or lock_path()
    with exclusive_lock(lock_target):
        try:
            target.unlink()
            return True
        except FileNotFoundError:
            return False

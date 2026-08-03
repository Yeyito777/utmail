from __future__ import annotations

import base64
import json
import time

from utmail_tool.session import OWA_APP_ID, OWA_AUDIENCE, OWA_BASE_URL, SESSION_VERSION, OwaSession


def encoded(value: dict) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def token(*, expires: int | None = None, marker: str = "secret-marker") -> str:
    claims = {
        "aud": OWA_AUDIENCE,
        "appid": OWA_APP_ID,
        "tid": "tenant-id",
        "oid": "object-id",
        "scp": "Mail.ReadWrite OWA.AccessAsUser.All",
        "exp": expires if expires is not None else int(time.time()) + 86_400,
        "marker": marker,
    }
    return f"{encoded({'alg':'none'})}.{encoded(claims)}.{encoded({'sig':marker})}"


def session(*, access_token: str | None = None) -> OwaSession:
    raw = access_token or token()
    return OwaSession(
        version=SESSION_VERSION,
        access_token=raw,
        mailbox="student@example.edu",
        base_url=OWA_BASE_URL,
        imported_at=int(time.time()),
        expires_at=int(time.time()) + 86_400,
        tenant_id="tenant-id",
        object_id="object-id",
        app_id=OWA_APP_ID,
        source="test",
    )

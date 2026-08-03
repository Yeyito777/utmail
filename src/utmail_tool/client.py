from __future__ import annotations

import json
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

import requests

from .errors import NetworkError, SessionRejectedError
from .session import OWA_BASE_URL, OwaSession


API_PREFIX = "/api/v2.0/"
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024
MAX_PAGES = 10
MAX_ITEMS = 100
RETRY_STATUSES = {429, 502, 503, 504}


@dataclass(frozen=True)
class BinaryResponse:
    content: bytes
    content_type: str | None
    content_disposition: str | None


class OwaClient:
    def __init__(
        self,
        owa_session: OwaSession,
        *,
        transport: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        owa_session.assert_current()
        self.owa_session = owa_session
        self.transport = transport or requests.Session()
        self.sleeper = sleeper

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.owa_session.access_token}",
            "Accept": "application/json",
            "Prefer": 'IdType="ImmutableId", outlook.body-content-type="text"',
            "X-AnchorMailbox": f"SMTP:{self.owa_session.mailbox}",
            "X-Req-Source": "Mail",
            "X-MS-AppName": "owa-reactmail",
        }

    @staticmethod
    def validate_url(value: str) -> str:
        if value.startswith(API_PREFIX):
            value = OWA_BASE_URL + value
        try:
            parsed = urlsplit(value)
        except ValueError:
            raise NetworkError("Outlook returned an invalid API URL") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname != "outlook.cloud.microsoft"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise NetworkError("refused an Outlook API URL outside the allowed origin")
        decoded_path = unquote(parsed.path)
        if not decoded_path.startswith(API_PREFIX) or "/../" in decoded_path or decoded_path.endswith("/.."):
            raise NetworkError("refused an Outlook API URL outside the allowed read-only prefix")
        return value

    @staticmethod
    def _retry_delay(response: requests.Response, attempt: int) -> float:
        value = response.headers.get("Retry-After", "")
        try:
            delay = float(value)
        except ValueError:
            try:
                moment = parsedate_to_datetime(value).timestamp()
                delay = max(0.0, moment - time.time())
            except (TypeError, ValueError, OverflowError):
                delay = float(2**attempt)
        return min(max(delay, 0.0), 5.0)

    def _request(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        max_bytes: int,
    ) -> tuple[requests.Response, bytes]:
        url = self.validate_url(url)
        for attempt in range(3):
            try:
                response = self.transport.get(
                    url,
                    params=params,
                    headers=self._headers(),
                    timeout=(10, 30),
                    allow_redirects=False,
                    stream=True,
                )
            except requests.RequestException:
                if attempt < 2:
                    self.sleeper(float(2**attempt))
                    continue
                raise NetworkError("could not reach Outlook Web") from None
            if 300 <= response.status_code < 400:
                response.close()
                raise NetworkError("refused an Outlook API redirect")
            if response.status_code in (401, 403):
                response.close()
                raise SessionRejectedError(
                    "the OWA session was rejected; run `utmail login --persistent` if automatic renewal cannot recover it"
                )
            if response.status_code in RETRY_STATUSES and attempt < 2:
                delay = self._retry_delay(response, attempt)
                response.close()
                self.sleeper(delay)
                continue
            data = bytearray()
            try:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise NetworkError("Outlook response exceeded the configured safety limit")
            finally:
                response.close()
            if response.status_code >= 400:
                detail = ""
                try:
                    payload = json.loads(data)
                    error = payload.get("error", {}) if isinstance(payload, dict) else {}
                    code = str(error.get("code") or "").strip()
                    message = str(error.get("message") or "").strip()
                    detail = ": ".join(part for part in (code, message) if part)[:300]
                except (json.JSONDecodeError, UnicodeError):
                    pass
                raise NetworkError(
                    f"Outlook returned HTTP {response.status_code}" + (f": {detail}" if detail else "")
                )
            return response, bytes(data)
        raise NetworkError("Outlook request exhausted its retry budget")

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        _, data = self._request(url, params=params, max_bytes=MAX_JSON_BYTES)
        try:
            value = json.loads(data)
        except (json.JSONDecodeError, UnicodeError):
            raise NetworkError("Outlook returned an unexpected non-JSON response") from None
        if not isinstance(value, dict):
            raise NetworkError("Outlook returned an unexpected JSON response")
        return value

    def get_bytes(self, url: str, *, max_bytes: int = MAX_ATTACHMENT_BYTES) -> BinaryResponse:
        response, data = self._request(url, max_bytes=max_bytes)
        return BinaryResponse(
            content=data,
            content_type=response.headers.get("Content-Type"),
            content_disposition=response.headers.get("Content-Disposition"),
        )

    def collect(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= MAX_ITEMS:
            raise NetworkError(f"item limit must be between 1 and {MAX_ITEMS}")
        rows: list[dict[str, Any]] = []
        next_url: str | None = url
        next_params = params
        pages = 0
        while next_url and len(rows) < limit:
            pages += 1
            if pages > MAX_PAGES:
                raise NetworkError("Outlook pagination exceeded the safety limit")
            payload = self.get_json(next_url, params=next_params)
            next_params = None
            values = payload.get("value", [])
            if not isinstance(values, list):
                raise NetworkError("Outlook returned an invalid result collection")
            rows.extend(row for row in values if isinstance(row, dict))
            candidate = payload.get("@odata.nextLink")
            if candidate is not None and not isinstance(candidate, str):
                raise NetworkError("Outlook returned an invalid pagination link")
            next_url = self.validate_url(candidate) if candidate else None
        return rows[:limit]

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from .client import MAX_ITEMS, OwaClient
from .errors import UsageError


SUMMARY_FIELDS = (
    "Id,ConversationId,Subject,From,Sender,ReceivedDateTime,SentDateTime,"
    "IsRead,HasAttachments,Importance,BodyPreview,InternetMessageId"
)
DETAIL_FIELDS = SUMMARY_FIELDS + ",ToRecipients,CcRecipients,BccRecipients,ReplyTo,Body"

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_HREF_RE = re.compile(
    r"<a\b[^>]*?\bhref\s*=\s*(?P<quote>[\"'])(?P<url>.*?)(?P=quote)[^>]*>"
    r"(?P<label>.*?)</a\s*>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]*>")
_SPACE_RE = re.compile(r"\s+")
_DISCLAIMER_RE = re.compile(
    r"(?im)^[ \t]*(?:confidentiality\s+(?:notice|disclaimer)|privileged and confidential|"
    r"this (?:e-?mail|email|message|communication)(?: and any attachments)? "
    r"(?:is|are|may (?:be|contain)|contains|is intended)|"
    r"the information contained in this (?:e-?mail|email|message))\b"
)
_SIGNATURE_DELIMITER_RE = re.compile(r"(?m)^[ \t]*--[ \t]*$")
_INSTITUTION_RE = re.compile(
    r"(?i)\b(?:university of toronto|utoronto\.ca|faculty of |department of |"
    r"school of |college of |institute of )\b"
)
_CONTACT_RE = re.compile(
    r"(?im)(?:\b(?:tel(?:ephone)?|phone|fax|office|email|web|www)\s*[:.]|"
    r"https?://|\b\d{3}[-.) ]+\d{3}[-. ]+\d{4}\b|\b(?:street|st\.|avenue|ave\.|road|rd\.)\b)"
)


def parse_duration(value: str) -> timedelta:
    match = re.fullmatch(r"\s*(\d+)\s*([mhdw])\s*", value, re.IGNORECASE)
    if not match:
        raise UsageError("duration must look like 30m, 12h, 2d, or 3w")
    amount = int(match.group(1))
    if amount <= 0:
        raise UsageError("duration must be positive")
    unit = match.group(2).lower()
    argument = {
        "m": {"minutes": amount},
        "h": {"hours": amount},
        "d": {"days": amount},
        "w": {"weeks": amount},
    }[unit]
    try:
        return timedelta(**argument)
    except OverflowError:
        raise UsageError("duration is too large") from None


def _address(value: Any) -> dict[str, str | None] | None:
    if not isinstance(value, dict):
        return None
    email = value.get("EmailAddress", value)
    if not isinstance(email, dict):
        return None
    address = email.get("Address") or email.get("address")
    name = email.get("Name") or email.get("name")
    if not address and not name:
        return None
    return {"name": str(name) if name else None, "address": str(address) if address else None}


def _addresses(value: Any) -> list[dict[str, str | None]]:
    if not isinstance(value, list):
        return []
    return [parsed for item in value if (parsed := _address(item)) is not None]


def normalize_message(value: dict[str, Any], *, include_body: bool = False) -> dict[str, Any]:
    body = value.get("Body") if isinstance(value.get("Body"), dict) else {}
    result = {
        "id": value.get("Id"),
        "conversationId": value.get("ConversationId"),
        "internetMessageId": value.get("InternetMessageId"),
        "subject": value.get("Subject") or "",
        "from": _address(value.get("From")),
        "sender": _address(value.get("Sender")),
        "receivedAt": value.get("ReceivedDateTime"),
        "sentAt": value.get("SentDateTime"),
        "isRead": value.get("IsRead"),
        "hasAttachments": value.get("HasAttachments"),
        "importance": value.get("Importance"),
        "bodyPreview": value.get("BodyPreview") or "",
    }
    if include_body:
        result.update(
            {
                "to": _addresses(value.get("ToRecipients")),
                "cc": _addresses(value.get("CcRecipients")),
                "bcc": _addresses(value.get("BccRecipients")),
                "replyTo": _addresses(value.get("ReplyTo")),
                "body": body.get("Content") or "",
                "bodyType": body.get("ContentType") or "Text",
            }
        )
    return result


def normalize_account(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": value.get("Id"),
        "displayName": value.get("DisplayName"),
        "emailAddress": value.get("EmailAddress"),
    }


def _received_at_or_none(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is None:
            # Outlook normally returns an offset. Treat an omitted offset as UTC
            # rather than making filtering depend on the host timezone.
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _threshold(since: timedelta) -> datetime:
    try:
        return datetime.now(timezone.utc) - since
    except OverflowError:
        raise UsageError("duration is too large") from None


def _clean_url(value: str) -> str:
    value = html.unescape(value.strip())
    value = value.rstrip(".,;:!?")
    pairs = ((")", "("), ("]", "["), ("}", "{"))
    changed = True
    while changed and value:
        changed = False
        for closer, opener in pairs:
            if value.endswith(closer) and value.count(closer) > value.count(opener):
                value = value[:-1]
                changed = True
    return value


def _safe_destination(value: str) -> str | None:
    """Return a SafeLinks destination without making any network request."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme.casefold() != "https" or not (
        hostname == "safelinks.protection.outlook.com"
        or hostname.endswith(".safelinks.protection.outlook.com")
    ):
        return None
    try:
        destinations = parse_qs(
            parsed.query, keep_blank_values=True, max_num_fields=100
        ).get("url", [])
    except ValueError:
        return None
    if not destinations:
        return None
    destination = destinations[0].strip()
    try:
        target = urlsplit(destination)
    except ValueError:
        return None
    if target.scheme.casefold() not in {"http", "https"} or not target.hostname:
        return None
    return destination


def _plain_text(value: str) -> str:
    return _SPACE_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", value))).strip()


def _link_context(body: str, start: int, end: int, label: str | None) -> str:
    before = _plain_text(body[max(0, start - 80):start])
    after = _plain_text(body[end:min(len(body), end + 80)])
    center = label or ""
    context = " ".join(part for part in (before, center, after) if part)
    return context[:240]


def extract_links(body: str) -> list[dict[str, Any]]:
    """Extract HTTP(S) links locally, in body order, with exact-URL deduplication."""
    candidates: list[tuple[int, int, int, str, str | None]] = []
    for match in _HREF_RE.finditer(body):
        label = _plain_text(match.group("label")) or None
        candidates.append((match.start(), 0, match.end(), match.group("url"), label))
    for match in _URL_RE.finditer(body):
        candidates.append((match.start(), 1, match.end(), match.group(0), None))
    candidates.sort(key=lambda item: (item[0], item[1]))

    links: list[dict[str, Any]] = []
    seen: set[str] = set()
    for start, _, end, raw_url, label in candidates:
        original = _clean_url(raw_url)
        try:
            parsed = urlsplit(original)
        except ValueError:
            continue
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            continue
        destination = _safe_destination(original)
        url = destination or original
        if url in seen:
            continue
        seen.add(url)
        links.append(
            {
                "url": url,
                "text": label,
                "context": _link_context(body, start, end, label),
                "decodedSafeLink": destination is not None,
            }
        )
    return links


def compact_body(body: str) -> tuple[str, dict[str, Any]]:
    """Conservatively remove a large, recognizable signature/disclaimer tail."""
    cutoff: int | None = None
    reason: str | None = None

    # Strong disclaimer phrases are considered only when they begin a sizeable
    # tail. This avoids deleting an ordinary sentence that merely discusses a
    # disclaimer.
    for match in _DISCLAIMER_RE.finditer(body):
        tail = body[match.start():]
        if len(tail) >= 400 and len([line for line in tail.splitlines() if line.strip()]) >= 5:
            cutoff = match.start()
            reason = "institutional-disclaimer"
            break

    # A signature is removed only with all four conservative signals: a
    # conventional delimiter, a large multiline tail, an institutional name,
    # and multiple contact/address indicators.
    for match in _SIGNATURE_DELIMITER_RE.finditer(body):
        tail = body[match.start():]
        if (
            len(tail) >= 600
            and len([line for line in tail.splitlines() if line.strip()]) >= 8
            and _INSTITUTION_RE.search(tail)
            and len(_CONTACT_RE.findall(tail)) >= 2
            and (cutoff is None or match.start() < cutoff)
        ):
            cutoff = match.start()
            reason = "institutional-signature"
            break

    if cutoff is None:
        return body, {"truncated": False, "removedCharacters": 0, "reason": None}
    compacted = body[:cutoff].rstrip()
    return compacted, {
        "truncated": True,
        "removedCharacters": len(body) - len(compacted),
        "reason": reason,
    }


def prepare_message(
    message: dict[str, Any], *, compact: bool = False, links: bool = False
) -> dict[str, Any]:
    result = dict(message)
    if compact:
        compacted, details = compact_body(str(result.get("body") or ""))
        result["body"] = compacted
        result["bodyCompaction"] = details
    if links:
        extracted = extract_links(str(result.get("body") or ""))
        result.pop("body", None)
        result["links"] = extracted
    return result


class Mailbox:
    def __init__(self, client: OwaClient):
        self.client = client

    def whoami(self) -> dict[str, Any]:
        payload = self.client.get_json(
            "/api/v2.0/me",
            params={"$select": "Id,DisplayName,EmailAddress"},
        )
        return normalize_account(payload)

    def inbox(
        self,
        *,
        limit: int = 20,
        since: timedelta | None = None,
        unread: bool = False,
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        if unread:
            filters.append("IsRead eq false")
        if since is not None:
            moment = _threshold(since)
            filters.append("ReceivedDateTime ge " + moment.isoformat().replace("+00:00", "Z"))
        params: dict[str, Any] = {
            "$top": str(min(limit, MAX_ITEMS)),
            "$select": SUMMARY_FIELDS,
            "$orderby": "ReceivedDateTime desc",
        }
        if filters:
            params["$filter"] = " and ".join(filters)
        values = self.client.collect(
            "/api/v2.0/me/mailfolders/inbox/messages",
            params=params,
            limit=limit,
        )
        return [normalize_message(value) for value in values]

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        since: timedelta | None = None,
        unread: bool = False,
    ) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            raise UsageError("search query cannot be empty")
        if len(query) > 500:
            raise UsageError("search query is too long")
        escaped = query.replace('"', '\\"')
        threshold = _threshold(since) if since is not None else None
        # Outlook search does not reliably compose $search and $filter. Fetch a
        # fixed, bounded candidate window only when local filtering is needed.
        candidate_limit = MAX_ITEMS if threshold is not None or unread else limit
        values = self.client.collect(
            "/api/v2.0/me/messages",
            params={
                "$top": str(min(candidate_limit, MAX_ITEMS)),
                "$select": SUMMARY_FIELDS,
                "$search": f'"{escaped}"',
            },
            limit=candidate_limit,
        )
        rows = [normalize_message(value) for value in values]
        if unread:
            rows = [row for row in rows if row.get("isRead") is False]
        if threshold is not None:
            rows = [
                row
                for row in rows
                if (received := _received_at_or_none(row.get("receivedAt"))) is not None
                and received >= threshold
            ]
        return rows[:limit]

    def show(
        self, message_id: str, *, compact: bool = False, links: bool = False
    ) -> dict[str, Any]:
        encoded = quote(message_id, safe="")
        payload = self.client.get_json(
            f"/api/v2.0/me/messages/{encoded}",
            params={"$select": DETAIL_FIELDS},
        )
        return prepare_message(normalize_message(payload, include_body=True), compact=compact, links=links)

    def thread(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
        compact: bool = False,
        links: bool = False,
    ) -> list[dict[str, Any]]:
        escaped = conversation_id.replace("'", "''")
        values = self.client.collect(
            "/api/v2.0/me/messages",
            params={
                "$top": str(min(limit, MAX_ITEMS)),
                "$select": DETAIL_FIELDS,
                "$filter": f"ConversationId eq '{escaped}'",
            },
            limit=limit,
        )
        rows = [
            prepare_message(normalize_message(value, include_body=True), compact=compact, links=links)
            for value in values
        ]
        # Outlook rejects the ConversationId restriction combined with an
        # order-by clause as InefficientFilter. Sort the bounded result locally.
        rows.sort(key=lambda row: str(row.get("receivedAt") or row.get("sentAt") or ""))
        return rows

    def attachment_rows(self, message_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        encoded = quote(message_id, safe="")
        return self.client.collect(
            f"/api/v2.0/me/messages/{encoded}/attachments",
            params={"$top": str(min(limit, MAX_ITEMS))},
            limit=limit,
        )

    @staticmethod
    def attachment_value_url(message_id: str, attachment_id: str) -> str:
        return (
            f"/api/v2.0/me/messages/{quote(message_id, safe='')}/attachments/"
            f"{quote(attachment_id, safe='')}/$value"
        )

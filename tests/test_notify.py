from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from helpers import session
from test_client import FakeResponse, FakeTransport
from utmail_tool import exocortex, notify
from utmail_tool.client import OwaClient
from utmail_tool.errors import BrowserImportError, NetworkError, SessionRejectedError, SessionRequiredError


MAILBOX = "student@example.edu"


def row(message_id: str, received: str, **overrides):
    value = {
        "Id": message_id,
        "ConversationId": f"conv-{message_id}",
        "InternetMessageId": f"<{message_id}@example.com>",
        "Subject": f"Subject {message_id}",
        "From": {"EmailAddress": {"Name": "Course Bot", "Address": "notifications@instructure.com"}},
        "Sender": {"EmailAddress": {"Name": "Course Bot", "Address": "notifications@instructure.com"}},
        "ReplyTo": [],
        "ToRecipients": [{"EmailAddress": {"Name": "Student", "Address": MAILBOX}}],
        "CcRecipients": [],
        "ReceivedDateTime": received,
        "SentDateTime": received,
        "IsRead": False,
        "IsDraft": False,
        "HasAttachments": False,
        "Importance": "Normal",
        "BodyPreview": "Preview   line\r\nsecond line",
        "InferenceClassification": "Focused",
        "Categories": ["Course"],
        "Flag": {"FlagStatus": "NotFlagged"},
    }
    value.update(overrides)
    return value


HEADERS = [
    {"Name": "Authentication-Results", "Value": "spf=pass (sender IP) smtp.mailfrom=x; dkim=pass; dmarc=pass action=none"},
    {"Name": "X-MS-Exchange-Organization-SCL", "Value": "1"},
    {"Name": "X-Microsoft-Antispam", "Value": "BCL:4;ARA:13230040"},
    {"Name": "X-MS-Exchange-Organization-MessageDirectionality", "Value": "Incoming"},
    {"Name": "X-MS-Exchange-Organization-AuthAs", "Value": "Anonymous"},
    {"Name": "List-Id", "Value": "<course.piazza.com>"},
    {"Name": "List-Unsubscribe", "Value": "<https://example.com/u>"},
    {"Name": "Auto-Submitted", "Value": "auto-generated"},
    {"Name": "Return-Path", "Value": "<bounce@bounce.piazza.com>"},
]


class FakeReader:
    def __init__(self):
        self.rows: list[dict] = []
        self.since_calls: list[str] = []
        self.header_error: Exception | None = None
        self.attachment_rows = [{"Name": "syllabus.pdf", "ContentType": "application/pdf", "Size": 1234, "IsInline": False}]

    def newest(self, limit=50):
        return sorted(self.rows, key=lambda r: r["ReceivedDateTime"], reverse=True)[:limit]

    def since(self, moment, limit=100):
        stamp = notify.iso(moment)
        self.since_calls.append(stamp)
        return sorted((r for r in self.rows if r["ReceivedDateTime"] >= stamp), key=lambda r: r["ReceivedDateTime"])[:limit]

    def headers(self, message_id):
        if self.header_error:
            raise self.header_error
        return HEADERS

    def attachments(self, message_id):
        return self.attachment_rows


class FakePublisher:
    def __init__(self):
        self.calls: list[dict] = []
        self.fail: Exception | str | None = None

    def __call__(self, tool, source_id, event_id, text, *, data=None, occurred_at=None):
        self.calls.append({"tool": tool, "source": source_id, "eventId": event_id, "text": text, "data": data, "occurredAt": occurred_at})
        if isinstance(self.fail, Exception):
            raise self.fail
        status = self.fail or "queued"
        return {"deliveries": [{"subscriptionId": "s", "convId": "c", "status": status}]}


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class NotifierHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = Path(self.tmp.name) / "state" / "notify-state.json"
        self.reader = FakeReader()
        self.publisher = FakePublisher()
        self.clock = Clock()
        self.mtime = [1.0]
        self.session_error: Exception | None = None
        self.logs: list[str] = []

    def loader(self):
        if self.session_error:
            raise self.session_error
        return session()

    def notifier(self, **kwargs):
        return notify.InboxNotifier(
            poll_seconds=60,
            state_file=self.state_file,
            session_loader=self.loader,
            reader_factory=lambda _s: self.reader,
            publisher=self.publisher,
            registrar=lambda: None,
            clock=self.clock,
            session_mtime=lambda: self.mtime[0],
            log=self.logs.append,
            **kwargs,
        )

    def message_events(self):
        return [c for c in self.publisher.calls if c["data"]["kind"] == "message"]


class CursorAndDedupeTests(NotifierHarness):
    def test_first_poll_primes_without_publishing(self):
        self.reader.rows = [row("a", "2026-10-12T10:00:00Z"), row("b", "2026-10-12T11:00:00Z")]
        n = self.notifier()
        self.assertEqual(n.step(), 60)
        self.assertEqual(self.publisher.calls, [])
        state = json.loads(self.state_file.read_text())
        self.assertEqual(state["cursor"], "2026-10-12T11:00:00Z")
        self.assertEqual(set(state["seen"]), {"a", "b"})
        self.assertEqual(os.stat(self.state_file).st_mode & 0o777, 0o600)

    def test_new_message_published_once_with_deterministic_id(self):
        self.reader.rows = [row("a", "2026-10-12T10:00:00Z")]
        n = self.notifier()
        n.step()
        self.reader.rows.append(row("b", "2026-10-12T10:05:00Z"))
        n.step()
        n.step()  # overlap window re-fetches b; seen set dedupes
        events = self.message_events()
        self.assertEqual([e["eventId"] for e in events], ["utmail:message:b"])
        self.assertEqual(events[0]["tool"], "utmail")
        self.assertEqual(events[0]["source"], "inbox")
        self.assertEqual(self.reader.since_calls[-1], "2026-10-12T09:50:00Z")

    def test_restart_neither_replays_nor_skips_mail_received_while_down(self):
        self.reader.rows = [row("a", "2026-10-12T10:00:00Z")]
        self.notifier().step()
        self.reader.rows.append(row("b", "2026-10-12T10:05:00Z"))
        self.notifier().step()
        # Down for two days; mail kept arriving.
        self.reader.rows += [row("c", "2026-10-13T08:00:00Z"), row("d", "2026-10-14T09:00:00Z")]
        self.notifier().step()
        self.assertEqual([e["eventId"] for e in self.message_events()], [
            "utmail:message:b", "utmail:message:c", "utmail:message:d",
        ])
        self.assertEqual(json.loads(self.state_file.read_text())["cursor"], "2026-10-14T09:00:00Z")

    def test_late_visible_message_inside_overlap_is_published(self):
        self.reader.rows = [row("a", "2026-10-12T10:00:00Z")]
        n = self.notifier()
        n.step()
        self.reader.rows.append(row("late", "2026-10-12T09:55:00Z"))
        n.step()
        self.assertEqual([e["eventId"] for e in self.message_events()], ["utmail:message:late"])

    def test_drafts_are_skipped(self):
        self.reader.rows = [row("a", "2026-10-12T10:00:00Z")]
        n = self.notifier()
        n.step()
        self.reader.rows.append(row("draft", "2026-10-12T10:01:00Z", IsDraft=True))
        n.step()
        self.assertEqual(self.message_events(), [])

    def test_failed_publish_is_retained_and_retried_with_same_event(self):
        self.reader.rows = [row("a", "2026-10-12T10:00:00Z")]
        n = self.notifier()
        n.step()
        self.reader.rows.append(row("b", "2026-10-12T10:05:00Z"))
        self.publisher.fail = exocortex.ExocortexError("exocortexd is not running")
        n.step()
        self.assertIn("b", json.loads(self.state_file.read_text())["pending"])
        self.publisher.fail = "failed"
        self.notifier().step()  # also survives restart
        self.publisher.fail = None
        m = self.notifier()
        m.step()
        m.step()
        ids = [e["eventId"] for e in self.message_events()]
        self.assertEqual(ids, ["utmail:message:b"] * 3)
        state = json.loads(self.state_file.read_text())
        self.assertEqual(state["pending"], {})
        self.assertIn("b", state["seen"])

    def test_full_batch_switches_to_catch_up_without_overlap(self):
        self.reader.rows = [row("seed", "2026-10-12T00:00:00Z")]
        n = self.notifier()
        n.step()
        self.reader.rows += [row(f"m{i:03}", f"2026-10-12T01:{i // 60:02}:{i % 60:02}Z") for i in range(150)]
        self.assertEqual(n.step(), 1.0)
        self.assertTrue(json.loads(self.state_file.read_text())["catchUp"])
        n.step()
        self.assertEqual(self.reader.since_calls[-1], "2026-10-12T01:01:38Z")  # seed + m000..m098 filled the batch
        self.assertEqual(len(self.message_events()), 150)
        self.assertEqual(len({e["eventId"] for e in self.message_events()}), 150)

    def test_seen_set_is_pruned_behind_the_cursor(self):
        self.reader.rows = [row("old", "2026-10-01T00:00:00Z")]
        n = self.notifier()
        n.step()
        self.reader.rows.append(row("new", "2026-10-12T10:00:00Z"))
        n.step()
        self.assertEqual(set(json.loads(self.state_file.read_text())["seen"]), {"new"})


class EventShapeTests(NotifierHarness):
    def test_message_event_data_and_text(self):
        self.reader.rows = [row("a", "2026-10-12T10:00:00Z")]
        n = self.notifier()
        n.step()
        self.reader.rows.append(row(
            "b", "2026-10-12T10:05:00Z",
            HasAttachments=True,
            Importance="High",
            CcRecipients=[{"EmailAddress": {"Name": "TA", "Address": "ta@utoronto.ca"}}],
            BodyPreview="x" * 1000,
        ))
        n.step()
        event = self.message_events()[0]
        data = event["data"]
        self.assertEqual(set(data), {
            "schemaVersion", "kind", "mailbox", "folder", "messageId", "conversationId", "internetMessageId",
            "subject", "from", "sender", "replyTo", "to", "cc", "recipientRole", "fromSelf", "receivedAt",
            "sentAt", "detectedAt", "preview", "hasAttachments", "attachments", "isRead", "importance",
            "inferenceClassification", "categories", "flagStatus", "headers",
        })
        self.assertEqual(data["schemaVersion"], 1)
        self.assertEqual(data["kind"], "message")
        self.assertEqual(data["from"], {"name": "Course Bot", "address": "notifications@instructure.com", "domain": "instructure.com"})
        self.assertEqual(data["cc"][0]["domain"], "utoronto.ca")
        self.assertEqual(data["recipientRole"], "to")
        self.assertFalse(data["fromSelf"])
        self.assertEqual(data["importance"], "high")
        self.assertEqual(data["inferenceClassification"], "focused")
        self.assertEqual(data["flagStatus"], "notFlagged")
        self.assertEqual(data["categories"], ["Course"])
        self.assertEqual(data["attachments"], [{"name": "syllabus.pdf", "contentType": "application/pdf", "size": 1234, "isInline": False}])
        self.assertLessEqual(len(data["preview"]), notify.MAX_PREVIEW)
        self.assertEqual(data["headers"]["listId"], "<course.piazza.com>")
        self.assertEqual(data["headers"]["authResults"], {"spf": "pass", "dkim": "pass", "dmarc": "pass"})
        self.assertEqual(data["headers"]["bulkComplaintLevel"], 4)
        self.assertEqual(data["headers"]["spamConfidenceLevel"], 1)
        self.assertEqual(data["headers"]["directionality"], "incoming")
        self.assertEqual(data["headers"]["returnPathDomain"], "bounce.piazza.com")
        self.assertEqual(event["occurredAt"], 1791799500000)
        text = event["text"]
        self.assertTrue(text.startswith("Inbox · Focused · unread · high importance [thread:conv-b]"))
        self.assertIn("→ Course Bot <notifications@instructure.com>: Subject b", text)
        self.assertIn("Attachments: syllabus.pdf", text)
        self.assertTrue(text.endswith("[msg:b]"))
        self.assertLess(len(text), 600)
        serialized = json.dumps(event)
        self.assertNotIn("secret-marker", serialized)
        self.assertLess(len(serialized), 100_000)

    def test_preview_whitespace_collapsed_and_self_mail_flagged(self):
        event = notify.build_message_event(
            row("a", "2026-10-12T10:00:00Z", From={"EmailAddress": {"Name": "Me", "Address": "Student@Example.edu"}}),
            mailbox=MAILBOX,
            detected_at=notify.datetime(2026, 10, 12, 10, 0, 30, tzinfo=notify.timezone.utc),
        )
        self.assertEqual(event["data"]["preview"], "Preview line second line")
        self.assertTrue(event["data"]["fromSelf"])
        self.assertIsNone(event["data"]["headers"])

    def test_utoronto_alias_recipient_and_cc_role(self):
        mailbox = "jane.doe@mail.utoronto.ca"
        to = [{"address": "list@utoronto.ca"}]
        self.assertEqual(notify.recipient_role(mailbox, to, [{"address": "Jane.Doe@utoronto.ca"}]), "cc")
        self.assertEqual(notify.recipient_role(mailbox, to, []), "none")

    def test_header_failure_degrades_but_publishes(self):
        self.reader.rows = [row("a", "2026-10-12T10:00:00Z")]
        n = self.notifier()
        n.step()
        self.reader.header_error = NetworkError("Outlook returned HTTP 400")
        self.reader.rows += [row("b", "2026-10-12T10:05:00Z"), row("c", "2026-10-12T10:06:00Z")]
        n.step()
        events = self.message_events()
        self.assertEqual(len(events), 2)
        self.assertIsNone(events[0]["data"]["headers"])
        self.assertEqual(sum("headers unavailable" in line for line in self.logs), 1)

    def test_header_auth_failure_is_not_swallowed(self):
        self.reader.rows = [row("a", "2026-10-12T10:00:00Z")]
        n = self.notifier()
        n.step()
        self.reader.header_error = SessionRejectedError("rejected")
        self.reader.rows.append(row("b", "2026-10-12T10:05:00Z"))
        n.step()
        self.assertEqual(self.message_events(), [])


class AuthAndBackoffTests(NotifierHarness):
    def auth_events(self):
        return [c for c in self.publisher.calls if c["data"]["kind"] == "auth_needed"]

    def test_auth_expiry_publishes_one_deduplicated_event_and_idles(self):
        self.reader.rows = [row("a", "2026-10-12T10:00:00Z")]
        self.notifier().step()
        self.session_error = SessionRequiredError("the named vimbrowser Outlook context needs human reauthentication")
        n = self.notifier()
        self.assertEqual(n.step(), 60)  # first failure is confirmed before alerting
        self.assertEqual(self.auth_events(), [])
        delays = []
        for _ in range(7):
            self.clock.now += 10_000
            delays.append(n.step())
        self.assertEqual(delays, [300, 600, 1200, 2400, 3600, 3600, 3600])
        events = self.auth_events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertTrue(event["eventId"].startswith("utmail:auth-needed:"))
        self.assertEqual(event["data"]["kind"], "auth_needed")
        self.assertEqual(event["data"]["reason"], "session_required")
        self.assertEqual(event["data"]["mailbox"], MAILBOX)
        self.assertEqual(event["data"]["action"], "utmail login --persistent")
        # A restart during the outage does not re-alert.
        restarted = self.notifier()
        restarted.step()
        restarted.step()
        self.assertEqual(len(self.auth_events()), 1)
        # Recovery clears the episode; a later outage alerts once more with a new ID.
        self.session_error = None
        restarted.step()
        self.assertIsNone(json.loads(self.state_file.read_text())["auth"])
        self.session_error = SessionRequiredError("expired again")
        self.clock.now += 50_000
        restarted.step()
        restarted.step()
        events = self.auth_events()
        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0]["eventId"], events[1]["eventId"])

    def test_single_rejection_is_confirmed_before_alerting(self):
        self.reader.rows = [row("a", "2026-10-12T10:00:00Z")]
        n = self.notifier()
        n.step()
        self.session_error = SessionRejectedError("the OWA session was rejected")
        n.step()
        self.session_error = None
        n.step()
        self.assertEqual(self.auth_events(), [])
        self.session_error = SessionRejectedError("the OWA session was rejected")
        n.step()
        n.step()
        self.assertEqual([e["data"]["reason"] for e in self.auth_events()], ["session_rejected"])

    def test_unpublished_auth_event_is_retried_with_same_id(self):
        self.session_error = SessionRequiredError("UTmail is not logged in")
        self.publisher.fail = exocortex.ExocortexError("exocortexd is not running")
        n = self.notifier()
        n.step()
        n.step()
        n.step()
        self.publisher.fail = None
        n.step()
        n.step()
        ids = [e["eventId"] for e in self.auth_events()]
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 1)
        self.assertTrue(json.loads(self.state_file.read_text())["auth"]["published"])

    def test_fresh_login_wakes_the_auth_wait(self):
        self.session_error = SessionRequiredError("UTmail is not logged in")
        n = self.notifier()
        n.step()
        n.step()
        self.assertFalse(n.due())
        self.mtime[0] = 2.0  # `utmail login --persistent` rewrote session.json
        self.assertTrue(n.due())

    def test_transient_errors_back_off_and_reset(self):
        self.reader.rows = [row("a", "2026-10-12T10:00:00Z")]
        n = self.notifier()
        n.step()
        self.session_error = NetworkError("could not reach Outlook Web")
        delays = [n.step() for _ in range(6)]
        self.assertEqual(delays, [60, 120, 240, 480, 900, 900])
        self.session_error = BrowserImportError("vimbrowser-cli is not installed or discoverable")
        n.step()
        self.assertIn("browser renewal failed", json.loads(self.state_file.read_text())["lastError"])
        self.session_error = None
        self.assertEqual(n.step(), 60)
        self.assertEqual(self.auth_events(), [])


class ReaderTests(unittest.TestCase):
    def test_reader_uses_get_only_bounded_queries(self):
        transport = FakeTransport([
            FakeResponse(200, {"value": [row("a", "2026-10-12T10:00:00Z")]}),
            FakeResponse(200, {"InternetMessageHeaders": HEADERS}),
            FakeResponse(200, {"value": []}),
        ])
        reader = notify.InboxReader(OwaClient(session(), transport=transport))
        reader.since(notify.datetime(2026, 10, 12, 9, 45, tzinfo=notify.timezone.utc))
        reader.headers("id/with+chars")
        reader.attachments("a")
        (url, kwargs), (header_url, header_kwargs), (attachment_url, attachment_kwargs) = transport.calls
        self.assertEqual(url, "https://outlook.cloud.microsoft/api/v2.0/me/mailfolders/inbox/messages")
        self.assertEqual(kwargs["params"]["$filter"], "ReceivedDateTime ge 2026-10-12T09:45:00Z")
        self.assertEqual(kwargs["params"]["$orderby"], "ReceivedDateTime asc")
        self.assertIn("InferenceClassification", kwargs["params"]["$select"])
        self.assertTrue(header_url.endswith("/me/messages/id%2Fwith%2Bchars"))
        self.assertEqual(header_kwargs["params"], {"$select": "InternetMessageHeaders"})
        self.assertEqual(attachment_kwargs["params"]["$select"], "Name,ContentType,Size,IsInline")
        self.assertTrue(attachment_url.endswith("/me/messages/a/attachments"))


class IpcTests(unittest.TestCase):
    def test_publish_sends_data_without_target_conversation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "d.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen(1)
            received = {}

            def serve():
                conn, _ = server.accept()
                with conn:
                    line = conn.makefile().readline()
                    received.update(json.loads(line))
                    conn.sendall(b'{"type":"unrelated","reqId":"x"}\n')
                    reply = {"type": "external_notification_publish_result", "reqId": received["reqId"], "deliveries": []}
                    conn.sendall((json.dumps(reply) + "\n").encode())

            thread = threading.Thread(target=serve)
            thread.start()
            with mock.patch.dict(os.environ, {"EXOCORTEX_SOCKET": path}):
                result = exocortex.publish_external_notification("utmail", "inbox", "utmail:message:a", "text", data={"schemaVersion": 1})
            thread.join()
            server.close()
        self.assertEqual(result["deliveries"], [])
        self.assertEqual(received["type"], "publish_external_notification")
        self.assertEqual(received["data"], {"schemaVersion": 1})
        self.assertNotIn("convId", received)
        self.assertNotIn("occurredAt", received)

    def test_missing_daemon_is_a_clean_error(self):
        with mock.patch.dict(os.environ, {"EXOCORTEX_SOCKET": "/nonexistent/exocortexd.sock"}):
            with self.assertRaises(exocortex.ExocortexError):
                exocortex.register_external_notification_source("utmail", notify.SOURCE)


if __name__ == "__main__":
    unittest.main()

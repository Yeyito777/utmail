from __future__ import annotations

import argparse
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from unittest.mock import patch

from utmail_tool import cli


class RecordingMailbox:
    def __init__(self):
        self.calls = []

    def search(self, query, *, limit, since, unread):
        self.calls.append(("search", query, limit, since, unread))
        return []

    def show(self, message_id, *, compact, links):
        self.calls.append(("show", message_id, compact, links))
        result = {
            "id": message_id,
            "conversationId": "thread",
            "subject": "Example",
            "from": {"name": "Sender", "address": "sender@example.edu"},
            "to": [],
            "receivedAt": "2026-08-01T00:00:00Z",
            "bodyType": "Text",
        }
        if links:
            result["links"] = [{
                "url": "https://example.edu",
                "text": "Example",
                "context": "Visit Example",
                "decodedSafeLink": True,
            }]
        else:
            result["body"] = "Body"
        if compact:
            result["bodyCompaction"] = {
                "truncated": True,
                "removedCharacters": 700,
                "reason": "institutional-signature",
            }
        return result

    def thread(self, conversation_id, *, limit, compact, links):
        self.calls.append(("thread", conversation_id, limit, compact, links))
        return [self.show("message", compact=compact, links=links)]


class CliViewTests(unittest.TestCase):
    def mailbox_patch(self, mailbox):
        return patch.object(cli, "_mailbox", return_value=(object(), mailbox, object()))

    def test_parser_accepts_composable_new_options(self):
        search = cli.parser().parse_args(["search", "query", "--since", "2d", "--unread"])
        self.assertEqual(search.since, "2d")
        self.assertTrue(search.unread)

        show = cli.parser().parse_args(["show", "message", "--links", "--compact", "--json"])
        self.assertTrue(show.links)
        self.assertTrue(show.compact)
        self.assertTrue(show.json)

        thread = cli.parser().parse_args(["thread", "conversation", "--links", "--compact"])
        self.assertTrue(thread.links)
        self.assertTrue(thread.compact)

    def test_search_command_passes_both_filters(self):
        mailbox = RecordingMailbox()
        args = argparse.Namespace(query="query", limit=4, since="2d", unread=True, json=False)
        with self.mailbox_patch(mailbox), redirect_stdout(io.StringIO()):
            cli.command_search(args)
        self.assertEqual(mailbox.calls, [("search", "query", 4, timedelta(days=2), True)])

    def test_link_json_has_schema_envelope_and_stable_link_fields(self):
        mailbox = RecordingMailbox()
        args = argparse.Namespace(message_id="message", compact=True, links=True, json=True)
        output = io.StringIO()
        with self.mailbox_patch(mailbox), redirect_stdout(output):
            cli.command_show(args)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertNotIn("body", payload["data"])
        self.assertEqual(
            set(payload["data"]["links"][0]),
            {"url", "text", "context", "decodedSafeLink"},
        )
        self.assertTrue(payload["data"]["bodyCompaction"]["truncated"])

    def test_human_link_and_compact_views_announce_decoding_and_removal(self):
        mailbox = RecordingMailbox()
        output = io.StringIO()
        args = argparse.Namespace(
            conversation_id="thread", limit=5, compact=True, links=True, json=False
        )
        with self.mailbox_patch(mailbox), redirect_stdout(output):
            cli.command_thread(args)
        rendered = output.getvalue()
        self.assertIn("=== Message 1 of 1 ===", rendered)
        self.assertIn("https://example.edu (decoded Outlook SafeLink)", rendered)
        self.assertIn("Compact mode excluded 700", rendered)

    def test_compacted_empty_body_does_not_fall_back_to_uncompacted_preview(self):
        message = {
            "subject": "Example",
            "from": None,
            "to": [],
            "body": "",
            "bodyPreview": "CONFIDENTIALITY NOTICE",
            "bodyCompaction": {
                "truncated": True,
                "removedCharacters": 500,
                "reason": "institutional-disclaimer",
            },
        }
        output = io.StringIO()
        with redirect_stdout(output):
            cli._print_message(message)
        self.assertNotIn("CONFIDENTIALITY NOTICE", output.getvalue())
        self.assertIn("Compact mode removed 500", output.getvalue())


if __name__ == "__main__":
    unittest.main()

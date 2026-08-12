from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any

from . import __version__
from .attachments import Attachments
from .client import OwaClient
from .errors import SessionRejectedError, UsageError, UtmailError
from .mail import Mailbox, parse_duration
from .renewal import load_or_refresh_session
from .session import DEFAULT_MAILBOX, OwaSession, delete_session, save_session
from .vimbrowser import VimbrowserAuthenticator, VimbrowserImporter


SCHEMA_VERSION = 1


def emit_json(value: Any) -> None:
    print(json.dumps({"schemaVersion": SCHEMA_VERSION, "data": value}, indent=2, ensure_ascii=False))


def emit_error(error: UtmailError, *, as_json: bool) -> None:
    if as_json:
        payload: dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "error": {
                "kind": type(error).__name__,
                "message": error.message,
                "exitCode": error.exit_code,
            },
        }
        if error.details:
            payload["error"]["details"] = error.details
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    else:
        print(f"utmail: {error.message}", file=sys.stderr)


def _address(value: Any) -> str:
    if not isinstance(value, dict):
        return "-"
    name = value.get("name")
    address = value.get("address")
    if name and address:
        return f"{name} <{address}>"
    return str(address or name or "-")


def _print_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No messages found.")
        return
    for row in rows:
        state = "unread" if row.get("isRead") is False else "read"
        attachment = " +attachment" if row.get("hasAttachments") else ""
        print(f"{row.get('receivedAt') or '-'}  [{state}{attachment}]")
        print(f"  From:    {_address(row.get('from'))}")
        print(f"  Subject: {row.get('subject') or '(no subject)'}")
        print(f"  ID:      {row.get('id') or '-'}")
        if row.get("conversationId"):
            print(f"  Thread:  {row['conversationId']}")


def _print_message_header(message: dict[str, Any]) -> None:
    print(f"Subject: {message.get('subject') or '(no subject)'}")
    print(f"From:    {_address(message.get('from'))}")
    print(f"To:      {', '.join(_address(value) for value in message.get('to', [])) or '-'}")
    if message.get("cc"):
        print(f"Cc:      {', '.join(_address(value) for value in message['cc'])}")
    print(f"Date:    {message.get('receivedAt') or message.get('sentAt') or '-'}")
    print(f"ID:      {message.get('id') or '-'}")
    print(f"Thread:  {message.get('conversationId') or '-'}")


def _print_message(message: dict[str, Any]) -> None:
    _print_message_header(message)
    compaction = message.get("bodyCompaction")
    body = message.get("body") if isinstance(compaction, dict) else (
        message.get("body") or message.get("bodyPreview") or ""
    )
    print("\n" + str(body or ""))
    if isinstance(compaction, dict) and compaction.get("truncated"):
        print(
            f"\n[Compact mode removed {compaction.get('removedCharacters', 0)} "
            f"signature/disclaimer characters.]"
        )


def _print_link_message(message: dict[str, Any]) -> None:
    _print_message_header(message)
    links = message.get("links")
    print()
    if not isinstance(links, list) or not links:
        print("No links found.")
    else:
        for index, link in enumerate(links, 1):
            decoded = " (decoded Outlook SafeLink)" if link.get("decodedSafeLink") else ""
            print(f"{index}. {link.get('url') or '-'}{decoded}")
            if link.get("text"):
                print(f"   Text:    {link['text']}")
            if link.get("context"):
                print(f"   Context: {link['context']}")
    compaction = message.get("bodyCompaction")
    if isinstance(compaction, dict) and compaction.get("truncated"):
        print(
            f"\n[Compact mode excluded {compaction.get('removedCharacters', 0)} "
            f"signature/disclaimer characters from link extraction.]"
        )


def _mailbox() -> tuple[OwaSession, Mailbox, Attachments]:
    owa_session = load_or_refresh_session()
    client = OwaClient(owa_session)
    mailbox = Mailbox(client)
    return owa_session, mailbox, Attachments(client, mailbox)


def command_login(args: argparse.Namespace) -> None:
    if not str(args.mailbox or "").strip():
        raise UsageError("--mailbox ADDRESS is required unless UTMAIL_MAILBOX is set")
    if args.persistent:
        if args.tab is not None:
            raise UsageError("--tab is valid only with --from-vimbrowser")
        if not args.json:
            print("Opening Outlook in the isolated UTmail vimbrowser context. Complete Microsoft/U of T sign-in if prompted.")
        session = VimbrowserAuthenticator().acquire(
            mailbox=args.mailbox,
            interactive=True,
        )
    elif args.from_vimbrowser:
        raw = VimbrowserImporter().import_bearer(tab_id=args.tab)
        source = f"vimbrowser-tab:{args.tab}" if args.tab is not None else "vimbrowser"
        session = OwaSession.from_token(raw, mailbox=args.mailbox, source=source)
    else:
        if args.tab is not None:
            raise UsageError("--tab is valid only with --from-vimbrowser")
        if args.token_stdin or not sys.stdin.isatty():
            raw = sys.stdin.read()
        else:
            print("Paste the OWA bearer. It will be validated, stored with mode 0600, and never printed.")
            raw = getpass.getpass("OWA bearer: ")
        source = "stdin"
        session = OwaSession.from_token(raw, mailbox=args.mailbox, source=source)
    account = Mailbox(OwaClient(session)).whoami()
    returned = str(account.get("emailAddress") or "").casefold()
    expected = session.mailbox.casefold()
    if returned != expected:
        raise SessionRejectedError("the imported OWA session belongs to a different mailbox")
    save_session(session)
    result = {**session.public(), "account": account}
    if args.json:
        emit_json(result)
    else:
        message = "Authenticated with a renewable Outlook session delegated to vimbrowser." if session.renewal_mode == "vimbrowser" else "Authenticated with an imported Outlook Web user session."
        print(message)
        print(f"  Mailbox: {session.mailbox}")
        print(f"  Expires: {result['expiresAt']}")
        if session.renewal_mode == "vimbrowser":
            print("  Renewal: automatic while Microsoft/U of T keeps the browser session valid")


def command_logout(args: argparse.Namespace) -> None:
    removed = delete_session()
    result = {"loggedOut": True, "sessionRemoved": removed, "vimbrowserSessionRetained": True}
    if args.json:
        emit_json(result)
    else:
        print("Removed UTmail's local credentials. The vimbrowser context remains signed in and is not removed.")


def command_status(args: argparse.Namespace) -> None:
    session, mailbox, _ = _mailbox()
    account = mailbox.whoami()
    result = {**session.public(), "account": account}
    if args.json:
        emit_json(result)
    else:
        print("Authenticated.")
        print(f"  Mailbox: {session.mailbox}")
        print(f"  Name:    {account.get('displayName') or '-'}")
        print(f"  Expires: {result['expiresAt']}")


def command_whoami(args: argparse.Namespace) -> None:
    _, mailbox, _ = _mailbox()
    result = mailbox.whoami()
    if args.json:
        emit_json(result)
    else:
        print(f"Name:    {result.get('displayName') or '-'}")
        print(f"Email:   {result.get('emailAddress') or '-'}")
        print(f"User ID: {result.get('id') or '-'}")


def command_inbox(args: argparse.Namespace) -> None:
    _, mailbox, _ = _mailbox()
    rows = mailbox.inbox(
        limit=args.limit,
        since=parse_duration(args.since) if args.since else None,
        unread=args.unread,
    )
    emit_json(rows) if args.json else _print_rows(rows)


def command_search(args: argparse.Namespace) -> None:
    _, mailbox, _ = _mailbox()
    rows = mailbox.search(
        args.query,
        limit=args.limit,
        since=parse_duration(args.since) if args.since else None,
        unread=args.unread,
    )
    emit_json(rows) if args.json else _print_rows(rows)


def command_show(args: argparse.Namespace) -> None:
    _, mailbox, _ = _mailbox()
    result = mailbox.show(args.message_id, compact=args.compact, links=args.links)
    if args.json:
        emit_json(result)
    elif args.links:
        _print_link_message(result)
    else:
        _print_message(result)


def command_thread(args: argparse.Namespace) -> None:
    _, mailbox, _ = _mailbox()
    rows = mailbox.thread(
        args.conversation_id,
        limit=args.limit,
        compact=args.compact,
        links=args.links,
    )
    if args.json:
        emit_json(rows)
        return
    if not rows:
        print("No messages found in that conversation.")
        return
    for index, row in enumerate(rows, 1):
        print(f"\n=== Message {index} of {len(rows)} ===")
        _print_link_message(row) if args.links else _print_message(row)


def command_attachments(args: argparse.Namespace) -> None:
    _, _, attachments = _mailbox()
    rows = attachments.list(args.message_id)
    if args.json:
        emit_json(rows)
        return
    if not rows:
        print("No attachments.")
        return
    for row in rows:
        print(f"{row.get('name')}  {row.get('size') or 0} bytes  {row.get('contentType')}")
        print(f"  ID: {row.get('id')}")


def command_download(args: argparse.Namespace) -> None:
    _, _, attachments = _mailbox()
    result = attachments.download(
        args.message_id,
        args.attachment_id,
        output_directory=args.out,
        force=args.force,
    )
    if args.json:
        emit_json(result)
    else:
        print(f"Downloaded privately: {result['path']} ({result['bytes']} bytes)")


def add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit stable JSON")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="utmail",
        description="Unofficial read-only UTmail CLI using a private Outlook Web user session.",
    )
    root.add_argument("--version", action="version", version=f"utmail {__version__}")
    commands = root.add_subparsers(dest="command", required=True)

    login = commands.add_parser("login", help="initialize or import an Outlook Web user session")
    source = login.add_mutually_exclusive_group(required=True)
    source.add_argument("--persistent", action="store_true", help="initialize a renewable session in UTmail's named vimbrowser context")
    source.add_argument("--from-vimbrowser", action="store_true", help="capture a current bearer from one exact Outlook Web tab")
    source.add_argument("--token-stdin", action="store_true", help="read a bare bearer, Authorization header, or access_token JSON from stdin")
    login.add_argument("--tab", type=int, help="exact vimbrowser Outlook tab ID")
    login.add_argument(
        "--mailbox",
        default=DEFAULT_MAILBOX,
        help="full U of T mailbox address (or set UTMAIL_MAILBOX)",
    )
    add_json(login)
    login.set_defaults(func=command_login)

    logout = commands.add_parser(
        "logout",
        help="delete only UTmail's local tokens; the vimbrowser context stays signed in",
    )
    add_json(logout)
    logout.set_defaults(func=command_logout)
    status = commands.add_parser("status", help="validate the saved session and account")
    add_json(status)
    status.set_defaults(func=command_status)
    whoami = commands.add_parser("whoami", help="show the authenticated mailbox account")
    add_json(whoami)
    whoami.set_defaults(func=command_whoami)

    inbox = commands.add_parser("inbox", help="list Inbox message summaries")
    inbox.add_argument("--limit", "-n", type=int, default=20)
    inbox.add_argument("--since", help="only messages newer than a duration such as 2d")
    inbox.add_argument("--unread", action="store_true")
    add_json(inbox)
    inbox.set_defaults(func=command_inbox)
    search = commands.add_parser("search", help="search mailbox message summaries")
    search.add_argument("query")
    search.add_argument("--limit", "-n", type=int, default=20)
    search.add_argument("--since", help="only matches received within a duration such as 2d")
    search.add_argument("--unread", action="store_true", help="only matches whose read state is unread")
    add_json(search)
    search.set_defaults(func=command_search)
    show = commands.add_parser("show", help="show one explicit message including its body")
    show.add_argument("message_id")
    show.add_argument("--links", action="store_true", help="show deduplicated links instead of the body")
    show.add_argument("--compact", action="store_true", help="conservatively remove a large signature/disclaimer tail")
    add_json(show)
    show.set_defaults(func=command_show)
    thread = commands.add_parser("thread", help="show an explicit conversation including message bodies")
    thread.add_argument("conversation_id")
    thread.add_argument("--limit", "-n", type=int, default=100)
    thread.add_argument("--links", action="store_true", help="show per-message deduplicated links instead of bodies")
    thread.add_argument("--compact", action="store_true", help="conservatively remove large signature/disclaimer tails")
    add_json(thread)
    thread.set_defaults(func=command_thread)
    attachments = commands.add_parser("attachments", help="list attachments on one message")
    attachments.add_argument("message_id")
    add_json(attachments)
    attachments.set_defaults(func=command_attachments)
    download = commands.add_parser("download", help="download one file attachment privately")
    download.add_argument("message_id")
    download.add_argument("attachment_id")
    download.add_argument("--out", required=True)
    download.add_argument("--force", action="store_true")
    add_json(download)
    download.set_defaults(func=command_download)
    return root


def main() -> None:
    args = parser().parse_args()
    as_json = bool(getattr(args, "json", False))
    try:
        if hasattr(args, "limit") and not 1 <= args.limit <= 100:
            raise UsageError("--limit must be between 1 and 100")
        args.func(args)
    except UtmailError as error:
        emit_error(error, as_json=as_json)
        raise SystemExit(error.exit_code)


if __name__ == "__main__":
    main()

# utmail

An unofficial, user-local, read-only CLI for a University of Toronto Outlook Web mailbox.

`utmail` delegates persistent Microsoft sign-in to a named, isolated [`vimbrowser`](https://github.com/Yeyito777/vimbrowser) context, imports the resulting first-party OWA session, and calls Outlook's authenticated HTTPS endpoints directly. It can list and search mail, display selected messages and threads, inspect attachments, and download an explicitly selected attachment without exposing mailbox mutation commands.

> [!WARNING]
> This project is not affiliated with, supported by, or endorsed by the University of Toronto or Microsoft. It relies on private Outlook Web behavior that may change without notice. Use it only with a mailbox you own and at human-scale request rates.

## Features

- Private persistent Microsoft/U of T sign-in through `vimbrowser-cli` and an isolated named context.
- Automatic access-token rotation.
- Context-backed browser-session recovery after Microsoft's approximately 24-hour browser-SPA refresh window.
- Read-only Inbox summaries and mailbox search, with composable age/unread filters.
- Explicit message-body and conversation retrieval, with opt-in link and compact-body views.
- Safe attachment listing and bounded private downloads.
- Stable JSON output for automation.
- Strict HTTPS origin/path allowlists, disabled redirects, bounded retries and response sizes.
- No send, reply, draft, delete, move, flag, mark-read, or settings commands.

## Security warning

The saved Outlook credentials and vimbrowser-owned context have broader server-side capabilities than this CLI exposes and may permit mailbox mutation if stolen. Read-only behavior is enforced by the local command and network surface, not by narrow OAuth scopes.

Authentication state is:

- never accepted through command-line arguments;
- never printed, logged, or included in JSON/errors;
- stored atomically—but not separately encrypted—in a mode-`0600` file beneath a mode-`0700` state directory;
- supplemented by a persistent context owned and protected by vimbrowser;
- removed from UTmail's local state by `utmail logout`; the vimbrowser context remains signed in.

This does not protect against malware or another process already running as your Unix user. See [SECURITY.md](SECURITY.md) for the complete boundary and revocation limitations.

## Requirements

- Linux (the implementation uses `fcntl` file locking)
- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- [`vimbrowser-cli`](https://github.com/Yeyito777/vimbrowser-cli) connected to a running vimbrowser with named-context support
- A graphical session for interactive authentication
- A University of Toronto Microsoft 365 mailbox

## Install

```bash
git clone https://github.com/Yeyito777/utmail.git
cd utmail
uv sync --locked
./utmail --help
```

The wrapper intentionally uses only the repository-local `.venv`.

## Login

Recommended persistent mode:

```bash
./utmail login --persistent --mailbox your.name@mail.utoronto.ca
```

Alternatively, set the mailbox once in the environment:

```bash
export UTMAIL_MAILBOX='your.name@mail.utoronto.ca'
./utmail login --persistent
```

`utmail` runs `vimbrowser-cli open-context utmail-helper https://outlook.cloud.microsoft/mail/`. A transient tab opens in that named persistent context, isolated from ordinary tabs and other contexts. Complete Microsoft/U of T login and MFA normally. Once Outlook finishes loading, `utmail` extracts the matched Outlook access/refresh credentials from that exact tab, validates `/api/v2.0/me`, saves its private local token state, closes only the transient tab, and restores the previously active tab where practical. The context's cookies and local storage remain owned by vimbrowser for later recovery.

Normal commands then:

1. use the current OWA access token while valid;
2. rotate it through Microsoft's fixed token endpoint when needed;
3. open a transient Outlook tab in the same named vimbrowser context after the browser-SPA refresh window ends;
4. request another interactive login only when Microsoft or U of T requires human reauthentication.

Renewal is best-effort, not literally permanent. Password changes, explicit revocation, Conditional Access, MFA policy, inactivity, or upstream changes can end a session.

### Short-lived compatibility imports

To capture only a short-lived bearer from an existing ordinary vimbrowser Outlook tab:

```bash
./utmail login --from-vimbrowser --tab TAB_ID --mailbox your.name@mail.utoronto.ca
```

A bearer can also be supplied through hidden/stdin input:

```bash
printf '%s' "$OWA_BEARER" | ./utmail login --token-stdin --mailbox your.name@mail.utoronto.ca
```

These compatibility modes save only an expiring access token and do not enable automatic renewal. Never place a bearer in argv, shell history, chat, an issue, or a log.

## Commands

```text
utmail login --persistent --mailbox ADDRESS [--json]
utmail login --from-vimbrowser --tab TAB_ID --mailbox ADDRESS [--json]
utmail login --token-stdin --mailbox ADDRESS [--json]
utmail logout [--json]
utmail status [--json]
utmail whoami [--json]
utmail inbox [--limit N] [--since DURATION] [--unread] [--json]
utmail search QUERY [--limit N] [--since DURATION] [--unread] [--json]
utmail show MESSAGE_ID [--links] [--compact] [--json]
utmail thread CONVERSATION_ID [--limit N] [--links] [--compact] [--json]
utmail attachments MESSAGE_ID [--json]
utmail download MESSAGE_ID ATTACHMENT_ID --out DIRECTORY [--force] [--json]
```

Examples:

```bash
./utmail status
./utmail inbox --since 2d --unread
./utmail search 'from:registrar@utoronto.ca enrolment' --since 2w --unread
./utmail show MESSAGE_ID
./utmail show MESSAGE_ID --links
./utmail thread CONVERSATION_ID --compact
./utmail thread CONVERSATION_ID --links --compact
./utmail attachments MESSAGE_ID
./utmail download MESSAGE_ID ATTACHMENT_ID --out ~/Downloads
```

Inbox and search output omit message bodies. Only explicit `show` and `thread` commands retrieve bodies. Downloads are capped at 100 MiB, written with mode `0600`, reject symlink output directories, and refuse overwrite unless `--force` is supplied.

### Search filter semantics

`--since DURATION` accepts a positive integer followed by `m`, `h`, `d`, or `w` (for example, `30m` or `2w`). It includes a result when its `ReceivedDateTime` is at or after the UTC instant calculated when the command starts. A result with a missing or invalid received timestamp does not pass `--since`. `--unread` includes only results whose Outlook `IsRead` value is exactly false. The two filters compose with AND.

Outlook does not reliably combine its mailbox `$search` operation with OData filters, so filtered search is deliberately bounded and deterministic: `utmail` asks Outlook for at most the first 100 candidates in Outlook's search ordering, applies `--since` and `--unread` locally without fetching message bodies, preserves candidate order, then emits at most `--limit` matches. An unfiltered search retains the existing behavior of requesting only `--limit` candidates. This fixed candidate window may return fewer than `--limit` matches even if matching mail exists later in the mailbox search results; it never causes an unbounded scan.

### Link view

`show --links` and `thread --links` replace each selected message's `body` field/view with a clean ordered `links` list. Only HTTP(S) URLs are extracted. Each JSON link has stable `url`, `text`, `context`, and `decodedSafeLink` fields. Exact destination duplicates are removed within a message while keeping the first body occurrence. Thread output intentionally keeps a destination when it occurs in different messages so its message-level context is not lost.

Microsoft Outlook SafeLinks are decoded entirely locally only when the wrapper uses HTTPS on the exact `safelinks.protection.outlook.com` host or one of its subdomains and its `url` parameter is an absolute HTTP(S) destination. A malformed wrapper, unsafe destination scheme, or lookalike host is left as the original HTTP(S) URL. The helper never opens, resolves, fetches, validates remotely, or follows an extracted link.

### Compact body view

`show --compact` and `thread --compact` opt into conservative deterministic tail removal. Default body and JSON output are unchanged. Compact mode removes only:

- a recognized confidentiality/disclaimer marker whose remaining tail is at least 400 characters and five non-empty lines; or
- a conventional `--` signature tail of at least 600 characters and eight non-empty lines that also contains an institutional identifier and at least two contact/address signals.

Ordinary short signatures, quoted content without those signals, and ordinary body text are retained. In compact JSON, `bodyCompaction` always reports `truncated`, `removedCharacters`, and `reason`; human output announces any removal. With `--compact --links`, links are extracted after compaction, `body` remains omitted, and `bodyCompaction` still records whether a footer was excluded.

## Read-only network boundary

Direct mailbox access is restricted to HTTPS `GET` requests under:

```text
https://outlook.cloud.microsoft/api/v2.0/
```

The client has no generic API escape hatch. Redirects are disabled, pagination URLs are revalidated, and request counts, retries, timeouts, response bytes, attachment bytes, and item counts are bounded.

Authentication renewal performs a fixed form-encoded `POST` to the Microsoft tenant token endpoint using Outlook Web's first-party client and `https://outlook.office.com/.default`. The named vimbrowser context may also load Microsoft, U of T SSO, and Outlook pages during reauthentication.

## Local state

Defaults:

```text
~/.local/state/utmail/session.json
~/.local/state/utmail/session.lock
```

Overrides used for testing or isolated deployments:

```text
UTMAIL_MAILBOX
UTMAIL_SESSION_FILE
UTMAIL_SESSION_LOCK_FILE
UTMAIL_VIMBROWSER_CLI
UTMAIL_VIMBROWSER_CONTEXT   # default: utmail-helper
```

`utmail logout` removes only the helper-owned token file. It does not delete or sign out the vimbrowser-owned context, revoke Microsoft server-side sessions, or sign out other browser contexts. Use vimbrowser/Microsoft account controls when that browser session must also be removed or revoked.

## JSON and exit codes

Successful JSON has `schemaVersion: 1` and a `data` value. Errors use the same schema with an `error` object. Neither contains credentials.

| Code | Meaning |
|---:|---|
| 0 | Success, including an empty result set |
| 1 | Unexpected helper failure |
| 2 | Invalid CLI usage |
| 3 | Login/session interaction required |
| 4 | Session rejected or belongs to another account |
| 5 | Outlook/network/protocol failure |
| 6 | Unsafe or invalid local file operation |
| 7 | Optional vimbrowser import failure |

## Development

```bash
uv sync --locked
.venv/bin/python -m unittest discover -s tests -v
```

The current suite covers token validation and redaction, private atomic storage, exact-tab vimbrowser import and context recovery, endpoint allowlisting, bounded retries, mail normalization, bounded/composable search filters, local SafeLinks decoding and link views, conservative compact-body projections, stable CLI output, safe attachments, refresh rotation, invalid-grant fallback, and concurrent renewal. Tests use injected fake vimbrowser runners and never interact with the user's live browser.

## License

[MIT](LICENSE)

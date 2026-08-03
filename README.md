# utmail

An unofficial, user-local, read-only CLI for a University of Toronto Outlook Web mailbox.

`utmail` owns a private persistent browser profile, imports the resulting first-party OWA session, and calls Outlook's authenticated HTTPS endpoints directly. It can list and search mail, display selected messages and threads, inspect attachments, and download an explicitly selected attachment without exposing mailbox mutation commands.

> [!WARNING]
> This project is not affiliated with, supported by, or endorsed by the University of Toronto or Microsoft. It relies on private Outlook Web behavior that may change without notice. Use it only with a mailbox you own and at human-scale request rates.

## Features

- Private persistent Microsoft/U of T sign-in through Playwright Chromium.
- Automatic access-token rotation.
- Headless browser-session recovery after Microsoft's approximately 24-hour browser-SPA refresh window.
- Read-only Inbox summaries and mailbox search.
- Explicit message-body and conversation retrieval.
- Safe attachment listing and bounded private downloads.
- Stable JSON output for automation.
- Strict HTTPS origin/path allowlists, disabled redirects, bounded retries and response sizes.
- No send, reply, draft, delete, move, flag, mark-read, or settings commands.

## Security warning

The saved Outlook credentials and browser profile have broader server-side capabilities than this CLI exposes and may permit mailbox mutation if stolen. Read-only behavior is enforced by the local command and network surface, not by narrow OAuth scopes.

Authentication state is:

- never accepted through command-line arguments;
- never printed, logged, or included in JSON/errors;
- stored atomically—but not separately encrypted—in a mode-`0600` file beneath a mode-`0700` state directory;
- supplemented by a dedicated Chromium profile whose root is mode `0700`;
- removed locally by `utmail logout`.

This does not protect against malware or another process already running as your Unix user. See [SECURITY.md](SECURITY.md) for the complete boundary and revocation limitations.

## Requirements

- Linux (the implementation uses `fcntl` file locking)
- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- An X11 session for initial interactive authentication
- A University of Toronto Microsoft 365 mailbox

## Install

```bash
git clone https://github.com/Yeyito777/utmail.git
cd utmail
uv sync --locked
uv run playwright install chromium
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

A dedicated Chromium window opens. Complete Microsoft/U of T login and MFA normally. Once Outlook finishes loading, `utmail` validates `/api/v2.0/me`, saves the private local session, and closes the window.

Normal commands then:

1. use the current OWA access token while valid;
2. rotate it through Microsoft's fixed token endpoint when needed;
3. start the dedicated profile headlessly after the browser-SPA refresh window ends;
4. request another interactive login only when Microsoft or U of T requires human reauthentication.

Renewal is best-effort, not literally permanent. Password changes, explicit revocation, Conditional Access, MFA policy, inactivity, or upstream changes can end a session.

### Short-lived compatibility imports

If [`vimbrowser-cli`](https://github.com/Yeyito777/vimbrowser-cli) is installed and Outlook is already authenticated there:

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
utmail search QUERY [--limit N] [--json]
utmail show MESSAGE_ID [--json]
utmail thread CONVERSATION_ID [--limit N] [--json]
utmail attachments MESSAGE_ID [--json]
utmail download MESSAGE_ID ATTACHMENT_ID --out DIRECTORY [--force] [--json]
```

Examples:

```bash
./utmail status
./utmail inbox --since 2d --unread
./utmail search 'from:registrar@utoronto.ca enrolment'
./utmail show MESSAGE_ID
./utmail thread CONVERSATION_ID
./utmail attachments MESSAGE_ID
./utmail download MESSAGE_ID ATTACHMENT_ID --out ~/Downloads
```

Inbox and search output omit message bodies. Only explicit `show` and `thread` commands display bodies. Downloads are capped at 100 MiB, written with mode `0600`, reject symlink output directories, and refuse overwrite unless `--force` is supplied.

## Read-only network boundary

Direct mailbox access is restricted to HTTPS `GET` requests under:

```text
https://outlook.cloud.microsoft/api/v2.0/
```

The client has no generic API escape hatch. Redirects are disabled, pagination URLs are revalidated, and request counts, retries, timeouts, response bytes, attachment bytes, and item counts are bounded.

Authentication renewal performs a fixed form-encoded `POST` to the Microsoft tenant token endpoint using Outlook Web's first-party client and `https://outlook.office.com/.default`. The helper-owned browser may also load Microsoft, U of T SSO, and Outlook pages during reauthentication.

## Local state

Defaults:

```text
~/.local/state/utmail/session.json
~/.local/state/utmail/session.lock
~/.local/state/utmail/browser-profile/
```

Overrides used for testing or isolated deployments:

```text
UTMAIL_MAILBOX
UTMAIL_SESSION_FILE
UTMAIL_SESSION_LOCK_FILE
UTMAIL_BROWSER_PROFILE
UTMAIL_VIMBROWSER_CLI
```

`utmail logout` removes the helper-owned token file and dedicated browser profile. It does not revoke Microsoft server-side sessions or sign out unrelated browsers.

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
uv run playwright install chromium
.venv/bin/python -m unittest discover -s tests -v
```

The current suite covers token validation and redaction, private atomic storage, exact optional browser import, endpoint allowlisting, bounded retries, mail normalization, safe attachments, refresh rotation, invalid-grant fallback, concurrent renewal, and browser-profile recovery.

## License

[MIT](LICENSE)

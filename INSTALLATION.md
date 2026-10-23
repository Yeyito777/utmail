# Installations

The same `main` runs on every machine. Machine-specific settings live in the
untracked `config/local.env`, a bash fragment that `./utmail` sources when it
exists. Write each setting as `VAR="${VAR:-value}"` so an explicit environment
variable still wins. Without the file, the defaults in this repository apply.

`manifest.json` (including its `notify run` daemon) is read only when this repo
is installed under an Exocortex `external-tools/` directory. A machine that
must not run the notifier sets `UTMAIL_NOTIFY_DAEMON=0`; `./utmail notify run`
then exits 0 at once, which the supervisor's `on-failure` policy does not
restart. Only one machine should poll a mailbox.

## whale (primary)

No `config/local.env`. Runs from `helper-tools/utmail` (styled by
`exo-manifest.json`; the daemon never scans it for `manifest.json`). The new-mail
notifier is the systemd user unit `utmail-notify.service`
(`./utmail notify run`); see NOTIFY.md.

## macOS Desktop Exocortex

Installed September 20, 2026 from the existing UofT workspace source.
Independent repository/runtime; no credentials are copied into source control.

- Manifest command `utmail`; `uoftmail` alias beside it.
- Python 3.13 isolated environment.
- Shared private state: `~/Desktop/uoft/.private/utmail`.
- Local `vimbrowser-cli`, never implicit SSH to whale.
- Default renewable context: `uoft-mac-mail`.
- Read-only, and no mailbox monitoring: whale runs the notifier.
- Use `login --from-vimbrowser --tab ID` with a signed-in Outlook tab or
  `login --persistent` for the dedicated renewable context.

`config/local.env` there:

```bash
umask 077
UTMAIL_NOTIFY_DAEMON=0
uoft="${UOFT_ROOT:-$HOME/Desktop/uoft}"
UTMAIL_MAILBOX="${UTMAIL_MAILBOX:-yeyito.linerobarcenas@mail.utoronto.ca}"
UTMAIL_SESSION_FILE="${UTMAIL_SESSION_FILE:-$uoft/.private/utmail/session.json}"
UTMAIL_SESSION_LOCK_FILE="${UTMAIL_SESSION_LOCK_FILE:-$uoft/.private/utmail/session.lock}"
UTMAIL_VIMBROWSER_CONTEXT="${UTMAIL_VIMBROWSER_CONTEXT:-uoft-mac-mail}"
UTMAIL_VIMBROWSER_CLI="${UTMAIL_VIMBROWSER_CLI:-vimbrowser-cli}"
```

Tests: `.venv/bin/python -m unittest discover -s tests`.

### October 15 upstream authentication update

- Existing local installation checkpointed at
  `local/pre-auth-update-20261015` (`dcbf305`).
- Integrated upstream auth commits `b99d40f`, `762b74f`, and `31df4ca`
  (from upstream tip `3028536`). Deliberately omitted unrelated notification
  poller/manifest changes: no background mailbox monitoring was enabled.
- Persistent-session re-login now supports Secrets and Duo's `own-login`
  protocol. It focuses a helper tab and performs MFA; do not invoke it when
  focus changes or that Duo protocol are disallowed. `UTMAIL_AUTOLOGIN=0`
  disables that fallback for safe silent-only probes.
- Silent context renewal closes its own helper tab without restoring stale
  focus; interactive login keeps upstream focus behavior. (Now on `main`.)
- A persistent session is not proof the context is still authenticated.

### October 22 reconciliation

The Mac-only commits (backup branch `mac-local`, `ca663d5`) were folded into
`main`. The Mac's `bin/utmail` wrapper became `config/local.env`
(`UTMAIL_NOTIFY_DAEMON=0` keeps the manifest's notifier off there), and
`bin/uoftmail` became `./uoftmail`. `manifest.json` keeps `./utmail` and
gained a portable system hint.

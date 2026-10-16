# Local Exocortex installation

Installed September 20, 2026 from the existing UofT workspace source.
Independent repository/runtime; no credentials are copied into source control.

- Manifest command `utmail`; `uoftmail` executable alias in the same bin directory.
- Python 3.13 isolated environment.
- Shared private state: `~/Desktop/uoft/.private/utmail`.
- Local `vimbrowser-cli`, never implicit SSH to whale.
- Default renewable context: `uoft-mac-mail`.
- Read-only: no sending/deleting messages or mailbox monitoring is installed.
- Use `login --from-vimbrowser --tab ID` with a signed-in Outlook tab or
  `login --persistent` for the dedicated renewable context.

Tests: `.venv/bin/python -m unittest discover -s tests`.

## October 15 upstream authentication update

- Existing local installation checkpointed at
  `local/pre-auth-update-20261015` (`dcbf305`).
- Integrated upstream auth commits `b99d40f`, `762b74f`, and `31df4ca`
  (from upstream tip `3028536`). Deliberately omitted unrelated notification
  poller/manifest changes: no background mailbox monitoring was enabled.
- Persistent-session re-login now supports Secrets and Duo's `own-login`
  protocol. It focuses a helper tab and performs MFA; do not invoke it when
  focus changes or that Duo protocol are disallowed. `UTMAIL_AUTOLOGIN=0`
  disables that fallback for safe silent-only probes.
- Local fix: silent context renewal closes its own helper tab without restoring
  stale focus; interactive login retains upstream focus behavior.
- A persistent session is not proof the context is still authenticated.
  Session/token paths, local browser backend, and context name remain unchanged.

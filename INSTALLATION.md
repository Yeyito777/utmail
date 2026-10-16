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

from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "utmail"


def session_path() -> Path:
    override = os.environ.get("UTMAIL_SESSION_FILE")
    return Path(override).expanduser() if override else state_dir() / "session.json"


def lock_path() -> Path:
    override = os.environ.get("UTMAIL_SESSION_LOCK_FILE")
    return Path(override).expanduser() if override else state_dir() / "session.lock"


def browser_profile_path() -> Path:
    override = os.environ.get("UTMAIL_BROWSER_PROFILE")
    return Path(override).expanduser() if override else state_dir() / "browser-profile"


def vimbrowser_cli() -> str:
    return os.environ.get("UTMAIL_VIMBROWSER_CLI", "vimbrowser-cli")

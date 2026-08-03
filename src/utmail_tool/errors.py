from __future__ import annotations


class UtmailError(Exception):
    exit_code = 1

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class UsageError(UtmailError):
    exit_code = 2


class SessionRequiredError(UtmailError):
    exit_code = 3


class SessionRejectedError(UtmailError):
    exit_code = 4


class NetworkError(UtmailError):
    exit_code = 5


class UnsafeFileError(UtmailError):
    exit_code = 6


class BrowserImportError(UtmailError):
    exit_code = 7

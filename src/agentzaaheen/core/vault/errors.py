"""Vault errors. Messages never contain secret values or key material."""

from __future__ import annotations

from enum import StrEnum


class VaultErrorCode(StrEnum):
    """Why the vault refused. Safe to log and show (reason code only)."""

    BAD_KEY_FILE = "vault.bad_key_file"
    KEY_FILE_TOO_OPEN = "vault.key_file_too_open"
    UNKNOWN_KEY = "vault.unknown_key"
    TAMPERED = "vault.tampered_or_wrong_slot"
    TOO_LARGE = "vault.secret_too_large"
    EMPTY = "vault.secret_empty"
    UNSUPPORTED = "vault.unsupported_format"


class VaultError(Exception):
    """The vault refused to do something. Always fail closed on this."""

    def __init__(self, code: VaultErrorCode, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code

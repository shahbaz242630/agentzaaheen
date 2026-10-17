"""Store errors: a reason code and a short message, never row contents."""

from __future__ import annotations

from enum import StrEnum


class StoreErrorCode(StrEnum):
    """Why a database call failed. Safe to log, audit and show."""

    UNAVAILABLE = "store.unavailable"
    CONFLICT = "store.conflict"
    REJECTED = "store.rejected"
    DENIED = "store.denied"
    FAILED = "store.failed"
    SCHEMA_MISMATCH = "store.schema_mismatch"
    MIGRATION_CHANGED = "store.migration_changed"
    NAIVE_TIME = "store.naive_time"


class StoreError(Exception):
    """A database call failed. ``code`` says why; the message never carries data values."""

    def __init__(self, code: StoreErrorCode, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code

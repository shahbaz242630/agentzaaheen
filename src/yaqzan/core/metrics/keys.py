"""The key-expiry rule: warn before a key dies, never fail mysteriously.

A key that expires within :data:`WARN_BEFORE` raises a P2; an expired key, or
one the vendor has just refused, raises a P1. A key with no recorded expiry
gets a P3 reminder to record one. The vendor's refusal outranks the date: a
revoked key is dead whatever its expiry says.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from yaqzan.core.sdk import Severity

WARN_BEFORE = timedelta(days=14)
REFUSED_CODES = frozenset({"net.auth", "source.auth"})


class KeyHealth(StrEnum):
    """One key's state. The value is also its catalogue code (``credential.<value>``)."""

    OK = "ok"
    EXPIRING = "expiring"
    EXPIRED = "expired"
    REFUSED = "refused"
    NO_DATE = "no_date"

    @property
    def code(self) -> str:
        """The error-catalogue code for this state."""
        return f"credential.{self.value}"

    @property
    def severity(self) -> Severity | None:
        """How loudly to tell the Chairman; ``None`` means nothing to say."""
        return _SEVERITY[self]


_SEVERITY: dict[KeyHealth, Severity | None] = {
    KeyHealth.OK: None,
    KeyHealth.EXPIRING: Severity.P2,
    KeyHealth.EXPIRED: Severity.P1,
    KeyHealth.REFUSED: Severity.P1,
    KeyHealth.NO_DATE: Severity.P3,
}


def key_health(expires_at: datetime | None, now: datetime, *, last_error_code: str | None) -> KeyHealth:
    """Judge one key from its expiry and the last error its source reported."""
    if now.tzinfo is None or (expires_at is not None and expires_at.tzinfo is None):
        raise ValueError("times must be timezone-aware")
    if last_error_code in REFUSED_CODES:
        return KeyHealth.REFUSED
    if expires_at is None:
        return KeyHealth.NO_DATE
    if expires_at <= now:
        return KeyHealth.EXPIRED
    if expires_at - now <= WARN_BEFORE:
        return KeyHealth.EXPIRING
    return KeyHealth.OK


def days_left(expires_at: datetime, now: datetime) -> int:
    """Whole days until ``expires_at`` (0 on the last day, negative once past)."""
    return (expires_at - now) // timedelta(days=1)

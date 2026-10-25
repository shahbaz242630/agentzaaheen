"""The freshness rule (PRD F1.7): old data shows "unknown", never "0".

A source stale for more than 2x its schedule turns grey (unknown); stale for
more than 6x also raises a P2. A source that has never synced is unknown too.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

STALE_AFTER = 2
ALERT_AFTER = 6


class Freshness(StrEnum):
    """How much a source's numbers can be trusted right now."""

    FRESH = "fresh"
    STALE = "stale"
    ALERT = "alert"
    NEVER = "never"

    @property
    def usable(self) -> bool:
        """Only fresh data may be shown as a number."""
        return self is Freshness.FRESH


def freshness(last_success: datetime | None, schedule: timedelta, now: datetime) -> Freshness:
    """Judge one source from its last successful sync and how often it should sync."""
    if schedule <= timedelta(0):
        raise ValueError("a schedule must be a positive interval")
    if now.tzinfo is None or (last_success is not None and last_success.tzinfo is None):
        raise ValueError("times must be timezone-aware")
    if last_success is None:
        return Freshness.NEVER
    age = now - last_success
    if age > schedule * ALERT_AFTER:
        return Freshness.ALERT
    if age > schedule * STALE_AFTER:
        return Freshness.STALE
    return Freshness.FRESH

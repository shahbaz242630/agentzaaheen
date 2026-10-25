"""Each workspace's daily summaries and the Board's briefings (migration 0011, F17 slice 6, ADR-039).

A summary is one workspace's facts for one day, as checked JSON: making the
same day again replaces it. A briefing is the tenant's one message for the
day: saved first as ``waiting``, then marked ``sent`` or ``not_sent`` with a
reason code; a briefing that was not sent may still be sent by a later try.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from agentzaaheen.core.store.db import Scoped, changed, require_aware
from agentzaaheen.core.store.errors import StoreError, StoreErrorCode

BriefingState = Literal["waiting", "sent", "not_sent"]
MAX_DAYS = 31

_PUT = """
INSERT INTO agentzaaheen.daily_summaries (tenant_id, project_id, day, made_at, facts)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (tenant_id, project_id, day) DO UPDATE SET made_at = EXCLUDED.made_at, facts = EXCLUDED.facts
"""
_RECENT = """
SELECT day, made_at, facts FROM agentzaaheen.daily_summaries
WHERE tenant_id = $1 AND project_id = $2 AND day <= $3
ORDER BY day DESC LIMIT $4
"""
_OPEN_BRIEFING = """
INSERT INTO agentzaaheen.briefings (tenant_id, day, made_at, title, message, state)
VALUES ($1, $2, $3, $4, $5, 'waiting')
"""
_SENT = """
UPDATE agentzaaheen.briefings SET state = 'sent', sent_at = $3, reason = NULL
WHERE tenant_id = $1 AND day = $2 AND state <> 'sent'
"""
_NOT_SENT = """
UPDATE agentzaaheen.briefings SET state = 'not_sent', reason = $3
WHERE tenant_id = $1 AND day = $2 AND state <> 'sent'
"""
_BRIEFINGS = """
SELECT day, made_at, title, message, state, sent_at, reason FROM agentzaaheen.briefings
WHERE tenant_id = $1 AND day <= $2 ORDER BY day DESC LIMIT $3
"""


@dataclass(frozen=True)
class SummaryRow:
    """One workspace's summary for one day, as saved (``facts`` is checked again when read)."""

    day: date
    made_at: datetime
    facts: dict[str, Any]


@dataclass(frozen=True)
class BriefingRow:
    """The Board's briefing for one day: the exact words, and whether they reached the phone."""

    day: date
    made_at: datetime
    title: str
    message: str
    state: BriefingState
    sent_at: datetime | None
    reason: str | None


def _limit(days: int) -> int:
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"ask for between 1 and {MAX_DAYS} days")
    return days


class DailySummaries:
    """The daily summaries of the one workspace of its handle."""

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope
        self._key = (scope.tenant_id, scope.require_workspace())

    async def put(self, day: date, facts: dict[str, Any], *, made_at: datetime) -> None:
        """Save the day's summary, replacing one made earlier the same day."""
        require_aware(made_at)
        async with self._scope.write("a daily summary") as conn:
            await conn.execute(_PUT, *self._key, day, made_at, facts)

    async def recent(self, *, until: date, days: int = 7) -> list[SummaryRow]:
        """Up to ``days`` summaries on or before ``until``, newest first."""
        async with self._scope.connection() as conn:
            rows = await conn.fetch(_RECENT, *self._key, until, _limit(days))
        return [SummaryRow(**dict(row)) for row in rows]


class Briefings:
    """The tenant's briefings (a tenant-level handle: the Board's, not one workspace's)."""

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope
        self._tenant = scope.tenant_id

    async def open(self, day: date, *, title: str, message: str, made_at: datetime) -> bool:
        """Save the day's briefing as waiting to be sent; ``False`` if that day already has one."""
        require_aware(made_at)
        try:
            async with self._scope.write("a briefing") as conn:
                await conn.execute(_OPEN_BRIEFING, self._tenant, day, made_at, title, message)
        except StoreError as exc:
            if exc.code is StoreErrorCode.CONFLICT:
                return False
            raise
        return True

    async def sent(self, day: date, *, at: datetime) -> bool:
        """Mark the day's briefing as sent. ``False`` if there is none, or it was already sent."""
        require_aware(at)
        async with self._scope.write("a briefing sent") as conn:
            return changed(await conn.execute(_SENT, self._tenant, day, at))

    async def not_sent(self, day: date, reason: str) -> bool:
        """Mark the day's briefing as not sent, with why. ``False`` if there is none, or it was sent."""
        async with self._scope.write("a briefing not sent") as conn:
            return changed(await conn.execute(_NOT_SENT, self._tenant, day, reason))

    async def recent(self, *, until: date, days: int = 7) -> list[BriefingRow]:
        """Up to ``days`` briefings on or before ``until``, newest first."""
        async with self._scope.connection() as conn:
            rows = await conn.fetch(_BRIEFINGS, self._tenant, until, _limit(days))
        return [BriefingRow(**dict(row)) for row in rows]

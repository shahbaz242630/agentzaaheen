"""Keeping each workspace's daily summary and the Board's briefing (F17 slice 6, ADR-039).

A summary is a read: code gathers what Yaqzan already holds and saves it;
nothing is switched on and nothing spends. The morning job (the Board's)
goes through the tenant's active workspaces one at a time, and the dashboard
reads one workspace's summaries by the API door's address. Either way this is
the door: the workspace comes from the server's own list, never from a
request body, and each summary is saved behind that workspace's wall.

The briefing is the Board's (tenant-level). Sending it is the one thing here
that leaves the laptop, so whether it went is written to the audit log
(``board.briefing``), with no words of the message.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from yaqzan.core.policy.audit import AuditDraft, AuditOutcome
from yaqzan.core.policy.ledger import Ledger
from yaqzan.core.sdk import get_logger, say
from yaqzan.core.store import BriefingRow, Briefings, DailySummaries, Database, SummaryRow, WorkspaceRow

SENT = "briefing.sent"
_ACTOR = "system:board-briefing"


class SummaryControl:
    """Saves and reads daily summaries, one workspace's handle at a time, and the tenant's briefings."""

    def __init__(self, db: Database, tenant: UUID, clock: Callable[[], datetime]) -> None:
        self._db = db
        self._tenant = tenant
        self._clock = clock
        self._ledger = Ledger(db, tenant)
        self._briefings = Briefings(db.scoped(tenant))
        self._log = get_logger("yaqzan.summaries")

    def _summaries(self, workspace: WorkspaceRow) -> DailySummaries:
        # The workspace comes from the server's own list (the job) or the API door: this is its door.
        return DailySummaries(self._db.scoped(self._tenant, workspace.workspace_id))

    async def save(self, workspace: WorkspaceRow, day: date, facts: dict[str, Any]) -> None:
        """Save the workspace's summary for ``day`` (replacing one made earlier that day)."""
        await self._summaries(workspace).put(day, facts, made_at=self._clock())
        say(self._log, "summary_saved", workspace=workspace.slug)

    async def recent(self, workspace: WorkspaceRow, *, until: date, days: int = 7) -> list[SummaryRow]:
        """The workspace's summaries on or before ``until``, newest first."""
        return await self._summaries(workspace).recent(until=until, days=days)

    async def before(self, workspace: WorkspaceRow, day: date) -> SummaryRow | None:
        """The newest summary from before ``day`` (to say what changed), or ``None``."""
        found = await self._summaries(workspace).recent(until=day - timedelta(days=1), days=1)
        return found[0] if found else None

    async def open_briefing(self, day: date, *, title: str, message: str) -> bool:
        """Save the day's briefing as waiting; ``False`` if the day already has one."""
        return await self._briefings.open(day, title=title, message=message, made_at=self._clock())

    async def briefing_sent(self, day: date) -> None:
        """The briefing reached ntfy: mark it and audit it."""
        await self._briefings.sent(day, at=self._clock())
        await self._audit(day, SENT)

    async def briefing_not_sent(self, day: date, reason: str) -> None:
        """The briefing could not be sent: mark it with why, and audit it (a later try may still send it)."""
        await self._briefings.not_sent(day, reason)
        await self._audit(day, reason)

    async def briefings(self, *, until: date, days: int = 7) -> list[BriefingRow]:
        """The tenant's briefings on or before ``until``, newest first."""
        return await self._briefings.recent(until=until, days=days)

    async def _audit(self, day: date, code: str) -> None:
        sent = code == SENT
        if sent:
            say(self._log, "briefing_sent", code=code)
        else:
            say(self._log, "briefing_not_sent", code=code)
        await self._ledger.record(
            AuditDraft(
                at=self._clock(),
                actor=_ACTOR,
                action="board.briefing",
                outcome=AuditOutcome.INFO,
                reason_code=code,
                correlation_id=uuid4(),
                target_ref=f"briefing:{day.isoformat()}",
                details={"day": day.isoformat(), "sent": sent},
            )
        )

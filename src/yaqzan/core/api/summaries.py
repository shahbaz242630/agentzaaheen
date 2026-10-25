"""Reading the daily summaries and the Board's briefings (F17 slice 6, ADR-039).

- ``GET /api/summaries``: the Board's cards, each workspace's newest summary in one line.
- ``GET /api/workspaces/{slug}/summaries``: one workspace's last 7 days in full (the door picks it).
- ``GET /api/briefings``: the Board's last 7 briefings, the exact words, and whether each was sent.
- ``POST /api/summaries/make``: make today's summaries again now (it reads and saves; nothing is sent).

Every summary was written by code from facts Agent Zaaheen holds; a saved one that no
longer reads as a summary is shown as unreadable, never guessed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date
from typing import Any, Literal, Protocol

from fastapi import Depends, FastAPI, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from yaqzan.core.api.summary import DailySummary, first_line, read_summary
from yaqzan.core.api.views import Health
from yaqzan.core.api.workspaces import InWorkspace
from yaqzan.core.store import BriefingRow, StoreError, SummaryRow, WorkspaceRow


class _View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SummaryLine(_View):
    """A workspace's newest summary as its Board card shows it. ``line`` is ``None`` if unreadable."""

    day: date
    made_at: AwareDatetime
    health: Health
    line: str | None = Field(max_length=400)


class SummaryOut(_View):
    """One day of one workspace. ``summary`` is ``None`` if the saved facts no longer read as one."""

    day: date
    made_at: AwareDatetime
    line: str | None = Field(max_length=400)
    summary: DailySummary | None


class WorkspaceSummariesOut(_View):
    """A workspace's last days, newest first. ``connected``: summaries are kept here (a database)."""

    connected: bool
    days: tuple[SummaryOut, ...]


class BoardSummariesOut(_View):
    """Each workspace's newest summary line, by its address; a workspace with none is left out."""

    connected: bool
    workspaces: dict[str, SummaryLine]


class BriefingOut(_View):
    """One morning briefing: the exact words, and whether they reached the phone."""

    day: date
    made_at: AwareDatetime
    title: str
    message: str
    state: Literal["waiting", "sent", "not_sent"]
    sent_at: AwareDatetime | None
    reason: str | None


class BriefingsOut(_View):
    """The Board's last briefings, newest first."""

    connected: bool
    briefings: tuple[BriefingOut, ...]


class MadeOut(_View):
    """Today's summaries made again: how many were saved and how many could not be made."""

    made: int = Field(ge=0)
    failed: int = Field(ge=0)


class WorkspaceSummaries(Protocol):
    """What the summary routes need (``LaptopSummaries`` in real use)."""

    async def newest(self) -> Mapping[str, SummaryRow]:
        """Each active workspace's newest summary, by its address."""
        ...

    async def recent(self, workspace: WorkspaceRow) -> Sequence[SummaryRow]:
        """One workspace's last 7 days, newest first."""
        ...

    async def briefings(self) -> Sequence[BriefingRow]:
        """The Board's last 7 briefings, newest first."""
        ...

    async def make_now(self) -> tuple[int, int]:
        """Make today's summaries again; how many were saved and how many failed."""
        ...


def summary_out(row: SummaryRow) -> SummaryOut:
    """One saved day as shown."""
    summary = read_summary(row.facts)
    return SummaryOut(
        day=row.day,
        made_at=row.made_at,
        line=None if summary is None else first_line(summary),
        summary=summary,
    )


def _line(row: SummaryRow) -> SummaryLine:
    summary = read_summary(row.facts)
    if summary is None:
        return SummaryLine(day=row.day, made_at=row.made_at, health=Health.UNKNOWN, line=None)
    return SummaryLine(day=row.day, made_at=row.made_at, health=summary.health, line=first_line(summary))


def _briefing(row: BriefingRow) -> BriefingOut:
    return BriefingOut(
        day=row.day,
        made_at=row.made_at,
        title=row.title,
        message=row.message,
        state=row.state,
        sent_at=row.sent_at,
        reason=row.reason,
    )


def summary_routes(
    app: FastAPI, summaries: WorkspaceSummaries | None, refuse_foreign: Callable[..., Any]
) -> None:
    """The routes for the Board's summary lines, a workspace's days, the briefings, and "make now"."""

    async def stored[T](call: Awaitable[T]) -> T:
        try:
            return await call
        except StoreError:
            raise HTTPException(status_code=503, detail="summaries.store_unavailable") from None

    @app.get("/api/summaries")
    async def board_summaries() -> BoardSummariesOut:
        if summaries is None:
            return BoardSummariesOut(connected=False, workspaces={})
        newest = await stored(summaries.newest())
        return BoardSummariesOut(connected=True, workspaces={s: _line(r) for s, r in newest.items()})

    @app.get("/api/workspaces/{slug}/summaries")
    async def workspace_summaries(workspace: InWorkspace) -> WorkspaceSummariesOut:
        if summaries is None:
            return WorkspaceSummariesOut(connected=False, days=())
        rows = await stored(summaries.recent(workspace))
        return WorkspaceSummariesOut(connected=True, days=tuple(summary_out(r) for r in rows))

    @app.get("/api/briefings")
    async def briefings() -> BriefingsOut:
        if summaries is None:
            return BriefingsOut(connected=False, briefings=())
        rows = await stored(summaries.briefings())
        return BriefingsOut(connected=True, briefings=tuple(_briefing(r) for r in rows))

    @app.post("/api/summaries/make", dependencies=[Depends(refuse_foreign)])
    async def make_now() -> MadeOut:
        if summaries is None:
            raise HTTPException(status_code=503, detail="summaries.not_connected")
        made, failed = await stored(summaries.make_now())
        return MadeOut(made=made, failed=failed)

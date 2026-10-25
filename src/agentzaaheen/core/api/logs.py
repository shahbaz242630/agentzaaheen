"""The Logs page's read: the kept log lines, newest first, filtered (ADR-034 slice 3).

``GET /api/logs`` reads the laptop's 30-day log files (``core.telemetry``) and
answers only what the log store may receive: the event code, its plain-English
``what``/``why``/``next``, and codes, names, counts and durations. Filters:
``level`` (this level and worse), ``days`` (1 to 30, today first), ``project``
(a workspace address or project id), ``agent``, ``trace`` (a trace ID, the
same one an error on screen leads to) and ``event``. Each filter has a strict
shape, so a request can only ask, never inject. Without kept logs (no
``--log-dir``) the answer says so: ``kept`` is false.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Protocol

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from agentzaaheen.core.telemetry.logfile import KEEP_DAYS
from agentzaaheen.core.telemetry.logread import DEFAULT_LINES, MAX_LINES, Level, LogPage, LogQuery, Value


class _View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LogLineOut(_View):
    """One kept line: when, how bad, what happened, why, what next, and the values it names."""

    timestamp: str | None
    level: Level
    event: str
    component: str | None
    what: str | None
    why: str | None
    next: str | None
    trace_id: str | None
    fields: dict[str, Value]


class LogsOut(_View):
    """The lines found, newest first. ``more``: more matched than were sent; ``cut``: a huge day was cut."""

    kept: bool
    lines: tuple[LogLineOut, ...] = ()
    more: bool = False
    cut: bool = False


class LogFilters(_View):
    """The page's filters; each has a strict shape, and anything else in the address is refused."""

    level: Level = "info"
    days: int = Field(default=1, ge=1, le=KEEP_DAYS)
    project: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")
    agent: str | None = Field(default=None, pattern=r"^[0-9a-f-]{1,64}$")
    trace: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    event: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    limit: int = Field(default=DEFAULT_LINES, ge=1, le=MAX_LINES)


class LogReader(Protocol):
    """What the route needs (``core.telemetry.LogFolder`` in real use)."""

    def read(self, query: LogQuery) -> LogPage:
        """The lines asked for, newest first (blocking)."""
        ...


_MAIN = ("timestamp", "level", "event", "component", "what", "why", "next", "trace_id", "span_id")


def _text(value: Value) -> str | None:
    return value if isinstance(value, str) else None


def line_out(line: dict[str, Value]) -> LogLineOut:
    """One shown line as the page gets it."""
    level = line.get("level")
    return LogLineOut(
        timestamp=_text(line.get("timestamp")),
        level=level if level in ("info", "warning", "error") else "info",
        event=str(line.get("event")),
        component=_text(line.get("component")),
        what=_text(line.get("what")),
        why=_text(line.get("why")),
        next=_text(line.get("next")) or None,
        trace_id=_text(line.get("trace_id")),
        fields={k: v for k, v in sorted(line.items()) if k not in _MAIN},
    )


def log_routes(app: FastAPI, logs: LogReader | None) -> None:
    """The route for the Logs page."""

    @app.get("/api/logs")
    async def read_logs(asked: Annotated[LogFilters, Query()]) -> LogsOut:
        if logs is None:
            return LogsOut(kept=False)
        query = LogQuery(**asked.model_dump())
        try:
            page = await asyncio.to_thread(logs.read, query)
        except OSError:
            raise HTTPException(status_code=503, detail="logs.unreadable") from None
        return LogsOut(kept=True, lines=tuple(line_out(x) for x in page.lines), more=page.more, cut=page.cut)

"""Reading the local log files back, newest first, filtered (ADR-034 slice 3: the Logs page, ``read-logs``).

A line read back keeps only what the log store may receive (:data:`SHOWN_FIELDS`:
the event code, its plain-English ``what``/``why``/``next``, codes, names,
counts and durations, the time, level and trace IDs), each a plain value.
Free text that a line may carry (a traceback, a path, tool arguments) stays
in the file, so whoever reads through here, the page or an agent, sees only
sentences built from our own templates and our own values.

Search is bounded: at most :data:`MAX_LINES` lines are returned, at most
:data:`KEEP_DAYS` days are searched, and only the newest
:data:`MAX_SCAN_BYTES` of a day's file; ``cut`` says when that last limit hid
older lines of a day. A line that is not JSON (a file cut short) is skipped.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

from yaqzan.core.telemetry.logfile import KEEP_DAYS, file_name
from yaqzan.core.telemetry.logs import LOG_FIELDS

type Level = Literal["info", "warning", "error"]
type Value = str | int | float | bool | None

LEVELS: tuple[Level, ...] = ("info", "warning", "error")
MAX_LINES = 500
DEFAULT_LINES = 200
MAX_SCAN_BYTES = 32 * 1024 * 1024
MAX_VALUE_CHARS = 400
SHOWN_FIELDS: frozenset[str] = LOG_FIELDS | {"timestamp", "level", "trace_id", "span_id"}
# A line belongs to a project when one of these names it.
_PROJECT_KEYS = ("project", "project_id", "workspace")


@dataclass(frozen=True)
class LogQuery:
    """Which lines to read: ``level`` and worse, from today and the ``days - 1`` days before."""

    level: Level = "info"
    days: int = 1
    project: str | None = None
    agent: str | None = None
    trace: str | None = None
    event: str | None = None
    limit: int = DEFAULT_LINES

    def __post_init__(self) -> None:
        if not 1 <= self.days <= KEEP_DAYS:
            raise ValueError(f"days must be 1 to {KEEP_DAYS}")
        if not 1 <= self.limit <= MAX_LINES:
            raise ValueError(f"limit must be 1 to {MAX_LINES}")

    def matches(self, line: dict[str, Value]) -> bool:
        """Whether a (shown) line is one asked for."""
        level = line.get("level")
        if level not in LEVELS or LEVELS.index(level) < LEVELS.index(self.level):
            return False
        if self.project is not None and self.project not in {line.get(k) for k in _PROJECT_KEYS}:
            return False
        checks = ((self.agent, "agent_id"), (self.trace, "trace_id"), (self.event, "event"))
        return all(wanted is None or line.get(key) == wanted for wanted, key in checks)


@dataclass(frozen=True)
class LogPage:
    """The lines found, newest first; ``more``: there were more than the limit; ``cut``: see the module."""

    lines: tuple[dict[str, Value], ...]
    more: bool
    cut: bool


def shown(raw: object) -> dict[str, Value] | None:
    """The part of one stored line that may be shown, or ``None`` if it is not a log line."""
    if not isinstance(raw, dict) or not isinstance(raw.get("event"), str):
        return None
    out: dict[str, Value] = {}
    for key, value in raw.items():
        if key not in SHOWN_FIELDS:
            continue
        if isinstance(value, str):
            out[key] = value[:MAX_VALUE_CHARS]
        elif value is None or isinstance(value, bool | int | float):
            out[key] = value
    return out


def read_logs(folder: Path, query: LogQuery, today: date) -> LogPage:
    """The lines ``query`` asks for, newest first."""
    found: list[dict[str, Value]] = []
    cut = False
    for back in range(query.days):
        path = folder / file_name(today - timedelta(days=back))
        if not path.is_file():
            continue
        wanted = query.limit + 1 - len(found)
        newest: deque[dict[str, Value]] = deque(maxlen=wanted)
        lines, was_cut = _tail(path)
        cut = cut or was_cut
        for text in lines:
            line = _parse(text)
            if line is not None and query.matches(line):
                newest.append(line)
        found.extend(reversed(newest))
        if len(found) > query.limit:
            break
    return LogPage(lines=tuple(found[: query.limit]), more=len(found) > query.limit, cut=cut)


def _tail(path: Path) -> tuple[Iterator[str], bool]:
    """The file's lines, only the newest :data:`MAX_SCAN_BYTES` of it; and whether older ones were left."""
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        start = max(0, size - MAX_SCAN_BYTES)
        handle.seek(start)
        data = handle.read()
    if start > 0:  # the first line is only part of one
        data = data.partition(b"\n")[2]
    return iter(data.decode("utf-8", errors="replace").splitlines()), start > 0


def _parse(text: str) -> dict[str, Value] | None:
    try:
        return shown(json.loads(text))
    except ValueError:
        return None


def _utc_today() -> date:
    return datetime.now(UTC).date()


class LogFolder:
    """The kept log files of one folder, read for the Logs page and the ``read-logs`` skill."""

    def __init__(self, folder: Path, *, today: Callable[[], date] = _utc_today) -> None:
        self._folder = folder
        self._today = today

    def read(self, query: LogQuery) -> LogPage:
        """The lines ``query`` asks for, newest first (blocking: file reads)."""
        return read_logs(self._folder, query, self._today())

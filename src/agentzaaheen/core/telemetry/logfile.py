"""The local log file: every redacted log line, kept for 30 days, one file a day (ADR-034 slice 3).

Once :meth:`LogFile.start` is called, the logger hands it every finished line,
after secrets, emails and card numbers are removed: the same line that goes
to the screen. Unlike the log
store, the file keeps every field: it never leaves the machine.

Files are named ``agentzaaheen-YYYY-MM-DD.jsonl`` (the UTC day of the lines in them),
one JSON line each. Opening a new day's file deletes the ones older than
:data:`KEEP_DAYS`. A day's file stops at :data:`MAX_DAY_BYTES`: one
``log_file_full`` line says so, and the rest of that day's lines are counted
as dropped, so a runaway loop can never fill the disk.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TextIO

from agentzaaheen.core.sdk import get_logger, install_log_sink, say

KEEP_DAYS = 30
MAX_DAY_BYTES = 200 * 1024 * 1024
_NAME = re.compile(r"^agentzaaheen-(\d{4}-\d{2}-\d{2})\.jsonl$")


def file_name(day: date) -> str:
    """The name of ``day``'s log file."""
    return f"agentzaaheen-{day.isoformat()}.jsonl"


def file_day(name: str) -> date | None:
    """The day a log file holds, or ``None`` for any other file."""
    match = _NAME.fullmatch(name)
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _utc_now() -> datetime:
    return datetime.now(UTC)


class LogFile:
    """Writes each redacted line to today's file. Safe to use from any thread."""

    def __init__(
        self,
        folder: Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
        keep_days: int = KEEP_DAYS,
        max_day_bytes: int = MAX_DAY_BYTES,
    ) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self._folder = folder
        self._clock = clock
        self._keep_days = keep_days
        self._max = max_day_bytes
        self._lock = threading.RLock()  # the "full" line is written from inside a write
        self._handle: TextIO | None = None
        self._day: date | None = None
        self._size = 0
        self._full = False
        self._noting = False
        self._dropped = 0

    @property
    def folder(self) -> Path:
        """Where the files are."""
        return self._folder

    @property
    def dropped(self) -> int:
        """Lines not kept because their day's file was full."""
        with self._lock:
            return self._dropped

    def __call__(self, line: Mapping[str, object]) -> None:
        """Keep one redacted log line (the logger's sink)."""
        with self._lock:
            if self._noting:  # the "full" line, on its way through every sink
                return
            day = self._clock().astimezone(UTC).date()
            if day != self._day:
                self._open(day)
            text = json.dumps(dict(line), sort_keys=True, ensure_ascii=False, default=str) + "\n"
            size = len(text.encode("utf-8"))
            if self._full or self._size + size > self._max:
                self._dropped += 1
                if not self._full:
                    self._full = True
                    self._say_full()
                return
            handle = self._handle
            if handle is not None:
                handle.write(text)
                handle.flush()
                self._size += size

    def start(self) -> LogFile:
        """Keep every log line from now on."""
        install_log_sink(self, "file")
        return self

    def stop(self) -> None:
        """Stop keeping log lines and close the file."""
        install_log_sink(None, "file")
        self.close()

    def close(self) -> None:
        """Close today's file; a later line opens it again."""
        with self._lock:
            if self._handle is not None:
                self._handle.close()
            self._handle, self._day = None, None

    def _open(self, day: date) -> None:
        self.close()
        path = self._folder / file_name(day)
        self._handle = path.open("a", encoding="utf-8", newline="\n")
        self._day = day
        self._size = path.stat().st_size
        self._full = self._size >= self._max
        self._tidy(day)

    def _tidy(self, today: date) -> None:
        for path in self._folder.iterdir():
            day = file_day(path.name)
            if day is not None and (today - day).days >= self._keep_days and path.is_file():
                path.unlink(missing_ok=True)

    def _say_full(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._noting = True
        try:  # written straight into the file, past the limit, and to the other sinks as usual
            say(get_logger("agentzaaheen.logfile", sink=handle), "log_file_full", bytes=self._max)
        finally:
            self._noting = False

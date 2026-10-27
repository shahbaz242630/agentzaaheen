"""Paddle, GitHub and Supabase read again every hour while the dashboard runs.

Each is read once at start (so the cards are real from the first page). Before
this, nothing read them again: every card turned "unknown" two hours later and
the 05:00 summaries said "Could not read" (found 27 Oct 2026). Each source keeps
its newest read in a :class:`Latest`, which its cards read on every request, so
freshness is still judged at the moment a card is shown. A re-read that fails
keeps the last good read, which turns "unknown" by its freshness rule; a
project the source answered for with a failure shows that failure.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from agentzaaheen.core.sdk import SourceError, get_logger, say, span

# Every card from these sources promises an hourly read (it turns "unknown" after two hours).
REREAD = timedelta(hours=1)


class Latest[T]:
    """One source's newest read, swapped whole so a page never sees half of a read."""

    def __init__(self, value: T) -> None:
        self.value = value


@dataclass(frozen=True)
class Reread:
    """One source's re-read: ``read`` swaps in a new read and returns how many projects could not be read."""

    source: str
    read: Callable[[], Awaitable[int]]


class HourlyReads:
    """Re-reads every source each ``interval_s`` on a background thread, like the uptime watcher."""

    def __init__(self, reads: Sequence[Reread], *, interval_s: float = REREAD.total_seconds()) -> None:
        self._reads = tuple(reads)
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._log = get_logger("agentzaaheen.reread")

    async def round(self) -> None:
        """Read every source once; one that fails keeps its last read and never stops the others."""
        with span("hourly re-read"):  # one trace per round: every outside call inside it
            for reread in self._reads:
                try:
                    failing = await reread.read()
                except SourceError as exc:
                    say(self._log, "reread_failed", source=reread.source, code=exc.reason_code)
                except Exception as exc:  # noqa: BLE001  # one odd source must never stop the rest
                    say(self._log, "reread_crashed", source=reread.source, kind=type(exc).__name__)
                else:
                    say(self._log, "reread_done", source=reread.source, failing=failing)

    def start(self) -> threading.Thread:
        """Re-read in the background until :meth:`stop` (a daemon thread: it ends with the dashboard)."""
        thread = threading.Thread(target=self._run, name="agentzaaheen-reread", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        """Stop re-reading after the current wait."""
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            asyncio.run(self.round())

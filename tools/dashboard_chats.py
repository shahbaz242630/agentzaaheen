"""The daily clean-up of old chats on this laptop (F19 slice 1; Chairman, 27 Oct 2026).

Chats are kept 12 months. While the dashboard runs, :class:`ChatSweep` removes
older ones once at start and then once a day, in every workspace on the
server's own list and the Board's. A failed sweep is logged and tried again
the next day; it never stops the dashboard.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence

from agentzaaheen.core.api.workspaces import WorkspaceDirectory
from agentzaaheen.core.sdk import get_logger, say
from agentzaaheen.core.store import WorkspaceRow

SWEEP_EVERY_S = 24 * 60 * 60.0

Forget = Callable[[Sequence[WorkspaceRow]], Awaitable[int]]


class ChatSweep:
    """Removes chats not used for 12 months: at start, then daily."""

    def __init__(
        self, forget: Forget, directory: WorkspaceDirectory, *, every_s: float = SWEEP_EVERY_S
    ) -> None:
        self._forget = forget
        self._directory = directory
        self._every_s = every_s
        self._task: asyncio.Task[None] | None = None
        self._log = get_logger("agentzaaheen.chats")

    async def sweep(self) -> None:
        """One clean-up; a failure is logged, never raised."""
        try:
            await self._forget(self._directory.all())
        except Exception as exc:  # noqa: BLE001  # a failed sweep must never end the sweeps
            say(self._log, "chats_sweep_failed", kind=type(exc).__name__)

    async def start(self) -> None:
        """Start the sweeps (inside the web server's event loop)."""
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        """Stop the sweeps."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while True:
            await self.sweep()
            await asyncio.sleep(self._every_s)

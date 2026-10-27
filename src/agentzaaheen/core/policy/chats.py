"""Keeping every chat as a record (F19 slice 1; Chairman, 27 Oct 2026).

This is the door: the workspace a chat is kept in comes from the API door's
address, or, for the Board's chat (no project open), is always Agent Zaaheen's
own workspace, never chosen by a request. Each turn is saved behind that
workspace's wall.

A conversation goes on while it is used; after :data:`IDLE_AFTER` without a
question the next one starts a new conversation (the old one stays on record).
Chats are kept :data:`KEEP_FOR`; the daily clean-up removes older ones, one
workspace at a time. Nothing here sends a chat anywhere, and nothing is used to
teach a model until a person clears it (F12, ADR-SEC-009).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from uuid import UUID

from agentzaaheen.core.sdk import get_logger, say
from agentzaaheen.core.store import ChatRows, ChatThread, Database, KeptTurn, StoreError, WorkspaceRow

IDLE_AFTER = timedelta(hours=3)  # Chairman, 27 Oct 2026
KEEP_FOR = timedelta(days=365)  # 12 months (Chairman, 27 Oct 2026)


class ChatControl:
    """Keeps and reads chat records, one workspace's handle at a time."""

    def __init__(self, db: Database, tenant: UUID, board: UUID, clock: Callable[[], datetime]) -> None:
        self._db = db
        self._tenant = tenant
        self._board = board
        self._clock = clock
        self._log = get_logger("agentzaaheen.chats")

    def _rows(self, workspace: WorkspaceRow | None) -> ChatRows:
        # The workspace comes from the API door's address; the Board's chat is always Agent Zaaheen's.
        if workspace is None:
            return ChatRows(self._db.scoped(self._tenant, self._board), board=True)
        return ChatRows(self._db.scoped(self._tenant, workspace.workspace_id))

    async def keep(
        self, workspace: WorkspaceRow | None, question: str, answer: str, *, model_id: str, from_sample: bool
    ) -> KeptTurn:
        """Keep one question and its answer in the conversation going on (or a new one)."""
        kept = await self._rows(workspace).add(
            question,
            answer,
            model_id=model_id,
            from_sample=from_sample,
            at=self._clock(),
            idle_after=IDLE_AFTER,
        )
        say(
            self._log,
            "chat_turn_kept",
            workspace="board" if workspace is None else workspace.slug,
            entries=kept.turn,
            stage="a new conversation" if kept.started else "the same conversation",
        )
        return kept

    async def going_on(self, workspace: WorkspaceRow | None) -> ChatThread | None:
        """The conversation the next question would join, with its turns; none if it would start one."""
        return await self._rows(workspace).going_on(self._clock(), IDLE_AFTER)

    async def forget_old(self, workspaces: Iterable[WorkspaceRow]) -> int:
        """Remove chats not used for :data:`KEEP_FOR` in each workspace (and the Board's); how many went.

        One workspace that cannot be cleaned is logged and the rest still are; it is tried again next day.
        """
        before = self._clock() - KEEP_FOR
        gone = 0
        ids = {self._board, *(w.workspace_id for w in workspaces)}
        for workspace_id in sorted(ids):
            try:
                # From the server's own list (and Agent Zaaheen's): this is its door.
                gone += await ChatRows(self._db.scoped(self._tenant, workspace_id)).forget_before(before)
            except StoreError as exc:
                say(self._log, "chats_forget_failed", code=exc.code.value)
        say(self._log, "chats_forgotten", entries=gone)
        return gone

"""Keeping every chat as a record, sealed (F19 slice 1; Chairman, 27 Oct 2026).

This is the door: the workspace a chat is kept in comes from the API door's
address, or, for the Board's chat (no project open), is always Agent Zaaheen's
own workspace, never chosen by a request. Each turn is saved behind that
workspace's wall.

The words are sealed before they reach the database (Chairman: "lock the
chats with the vault"), like the keys. This layer never touches the vault: the
sealing is done by the broker (``broker.ChatSeal``), handed in as a
:class:`TurnSeal`. If it cannot seal (no master key) nothing is kept at all: a
chat is never stored readable.

A conversation goes on while it is used; after :data:`IDLE_AFTER` without a
question the next one starts a new conversation (the old one stays on record).
Chats are kept :data:`KEEP_FOR`; the daily clean-up removes older ones, one
workspace at a time. Nothing here sends a chat anywhere, and nothing is used to
teach a model until a person clears it (F12, ADR-SEC-009).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from agentzaaheen.core.sdk import SourceError, get_logger, say
from agentzaaheen.core.store import ChatRows, Database, KeptTurn, StoreError, WorkspaceRow

IDLE_AFTER = timedelta(hours=3)  # Chairman, 27 Oct 2026
KEEP_FOR = timedelta(days=365)  # 12 months (Chairman, 27 Oct 2026)


class TurnSeal(Protocol):
    """Seals and opens chat turns (``broker.ChatSeal`` in real use). Failures leave as ``SourceError``."""

    def seal_turn(
        self, workspace: UUID, conversation: UUID, turn: int, question: str, answer: str
    ) -> tuple[bytes, bytes]:
        """The sealed question and answer."""
        ...

    def open_turn(
        self, workspace: UUID, conversation: UUID, turn: int, question: bytes, answer: bytes
    ) -> tuple[str, str]:
        """The words of a sealed turn."""
        ...


@dataclass(frozen=True)
class ChatTurn:
    """One question and its answer, opened."""

    turn: int
    at: datetime
    question: str
    answer: str
    model_id: str
    from_sample: bool


@dataclass(frozen=True)
class Conversation:
    """A conversation still going on, with its turns oldest first."""

    conversation_id: UUID
    last_at: datetime
    turns: tuple[ChatTurn, ...]


class ChatControl:
    """Keeps and reads chat records, sealed, one workspace's handle at a time."""

    def __init__(  # the database, the tenant, the Board's place, the sealing, the time
        self,
        db: Database,
        tenant: UUID,
        board: UUID,
        sealer: TurnSeal,
        clock: Callable[[], datetime],
    ) -> None:
        self._db = db
        self._tenant = tenant
        self._board = board
        self._sealer = sealer
        self._clock = clock
        self._log = get_logger("agentzaaheen.chats")

    def _where(self, workspace: WorkspaceRow | None) -> UUID:
        # The workspace comes from the API door's address; the Board's chat is always Agent Zaaheen's.
        return self._board if workspace is None else workspace.workspace_id

    def _rows(self, workspace: WorkspaceRow | None) -> ChatRows:
        return ChatRows(self._db.scoped(self._tenant, self._where(workspace)), board=workspace is None)

    async def keep(
        self, workspace: WorkspaceRow | None, question: str, answer: str, *, model_id: str, from_sample: bool
    ) -> KeptTurn | None:
        """Keep one question and its answer, sealed; ``None`` (and a log line) if it cannot be sealed."""
        name = "board" if workspace is None else workspace.slug
        where = self._where(workspace)

        def sealed(conversation: UUID, turn: int) -> tuple[bytes, bytes]:
            return self._sealer.seal_turn(where, conversation, turn, question, answer)

        try:
            kept = await self._rows(workspace).add(
                sealed, model_id=model_id, from_sample=from_sample, at=self._clock(), idle_after=IDLE_AFTER
            )
        except SourceError as exc:  # nothing was written: the transaction ended with the failure
            say(self._log, "chat_record_failed", workspace=name, code=str(exc.detail_code or exc.code))
            return None
        say(
            self._log,
            "chat_turn_kept",
            workspace=name,
            entries=kept.turn,
            stage="a new conversation" if kept.started else "the same conversation",
        )
        return kept

    async def going_on(self, workspace: WorkspaceRow | None) -> Conversation | None:
        """The conversation the next question would join, opened; none if it would start one.

        A turn that will not open (altered, moved, or no master key) raises ``SourceError``: never half-read.
        """
        thread = await self._rows(workspace).going_on(self._clock(), IDLE_AFTER)
        if thread is None:
            return None
        where, conversation = self._where(workspace), thread.conversation_id
        turns = []
        for kept in thread.turns:
            question, answer = self._sealer.open_turn(
                where, conversation, kept.turn, kept.question_sealed, kept.answer_sealed
            )
            turns.append(ChatTurn(kept.turn, kept.at, question, answer, kept.model_id, kept.from_sample))
        return Conversation(conversation, thread.last_at, tuple(turns))

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

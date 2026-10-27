"""Every chat kept as a record, per workspace (migration 0018, F19 slice 1).

A question and its answer are one turn of a conversation. The Board's chat is
kept in Agent Zaaheen's own workspace, apart from that project's own chat
(``board=True``). A turn joins the
workspace's newest conversation unless that one was ended on purpose or has
not been used for ``idle_after``; then a new conversation starts. Choosing the
conversation and adding the turn happen in one transaction (the conversation
row is locked), so two answers at once cannot take the same turn number.

The log line of each write names only the kind of record, never the words.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from agentzaaheen.core.store.db import Scoped, written

MAX_TURNS = 50  # read back at most this many turns of one conversation

_NEWEST = """
SELECT conversation_id, last_at FROM agentzaaheen.chat_conversations
WHERE tenant_id = $1 AND project_id = $2 AND for_board = $3 AND ended_at IS NULL
ORDER BY last_at DESC LIMIT 1
"""
_NEWEST_LOCKED = _NEWEST + "FOR UPDATE"
_START = """
INSERT INTO agentzaaheen.chat_conversations
    (tenant_id, project_id, conversation_id, for_board, started_at, last_at)
VALUES ($1, $2, $3, $4, $5, $5)
"""
_USED = """
UPDATE agentzaaheen.chat_conversations SET last_at = GREATEST(last_at, $4)
WHERE tenant_id = $1 AND project_id = $2 AND conversation_id = $3
"""
_NEXT_TURN = """
SELECT COALESCE(MAX(turn), 0) + 1 FROM agentzaaheen.chat_turns
WHERE tenant_id = $1 AND project_id = $2 AND conversation_id = $3
"""
_ADD_TURN = """
INSERT INTO agentzaaheen.chat_turns
    (tenant_id, project_id, conversation_id, turn, at, question, answer, model_id, from_sample)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
"""
_TURNS = """
SELECT turn, at, question, answer, model_id, from_sample FROM agentzaaheen.chat_turns
WHERE tenant_id = $1 AND project_id = $2 AND conversation_id = $3
ORDER BY turn DESC LIMIT 50
"""
_FORGET = "SELECT agentzaaheen.forget_old_chats($1)"


@dataclass(frozen=True)
class ChatTurn:
    """One question and its answer."""

    turn: int
    at: datetime
    question: str
    answer: str
    model_id: str
    from_sample: bool


@dataclass(frozen=True)
class KeptTurn:
    """Where a turn was kept: its conversation, its number, and whether it started that conversation."""

    conversation_id: UUID
    turn: int
    started: bool


@dataclass(frozen=True)
class ChatThread:
    """A conversation still going on, with its turns oldest first (at most :data:`MAX_TURNS`, the newest)."""

    conversation_id: UUID
    last_at: datetime
    turns: tuple[ChatTurn, ...]


class ChatRows:
    """The chat records of the handle's one workspace (or the Board's, kept in Agent Zaaheen's)."""

    def __init__(self, scope: Scoped, *, board: bool = False) -> None:
        self._scope = scope
        self._key = (scope.tenant_id, scope.require_workspace())
        self._board = board

    async def add(  # noqa: PLR0913  # the exchange, who answered, and when
        self,
        question: str,
        answer: str,
        *,
        model_id: str,
        from_sample: bool,
        at: datetime,
        idle_after: timedelta,
    ) -> KeptTurn:
        """Keep one turn in the conversation going on, or in a new one."""
        async with self._scope.write("a chat turn") as conn:
            newest = await conn.fetchrow(_NEWEST_LOCKED, *self._key, self._board)
            if newest is None or newest["last_at"] <= at - idle_after:
                conversation: UUID = uuid4()
                await conn.execute(_START, *self._key, conversation, self._board, at)
                turn, started = 1, True
            else:
                conversation = newest["conversation_id"]
                await conn.execute(_USED, *self._key, conversation, at)
                turn, started = await conn.fetchval(_NEXT_TURN, *self._key, conversation), False
            await conn.execute(
                _ADD_TURN, *self._key, conversation, turn, at, question, answer, model_id, from_sample
            )
        return KeptTurn(conversation, turn, started)

    async def going_on(self, at: datetime, idle_after: timedelta) -> ChatThread | None:
        """The conversation a question at ``at`` would join, with its turns; none if it would start one."""
        async with self._scope.connection() as conn:
            newest = await conn.fetchrow(_NEWEST, *self._key, self._board)
            if newest is None or newest["last_at"] <= at - idle_after:
                return None
            rows = await conn.fetch(_TURNS, *self._key, newest["conversation_id"])
        turns = tuple(ChatTurn(**dict(row)) for row in reversed(rows))
        return ChatThread(newest["conversation_id"], newest["last_at"], turns)

    async def forget_before(self, before: datetime) -> int:
        """Remove conversations last used before ``before`` (owner function); how many went."""
        async with self._scope.write("old chats removed") as conn:
            gone: int = await conn.fetchval(_FORGET, before)
            written(gone or None)  # none old enough: the line says nothing needed removing
        return gone

"""Chat records against a real Postgres (F19 slice 1).

The rules: every question and answer is kept behind its workspace's wall; a
conversation goes on until it has not been used for 3 hours; the Board's chat
is kept in Agent Zaaheen's own workspace apart from that project's chat; the
app can never change or delete a turn; chats not used for 12 months are
removed, and only those.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

import asyncpg
import pytest

from agentzaaheen.core.policy import ChatControl
from agentzaaheen.core.policy.chats import IDLE_AFTER, KEEP_FOR
from agentzaaheen.core.store import ChatRows, Database, StoreError, StoreErrorCode, WorkspaceRow
from tests.integration.conftest import ScratchDb, add_workspaces, connect_as, open_db, run
from tests.integration.test_runner import EVA, NOW, TENANT, ZAAHEEN

BOARD = UUID("00000000-0000-4000-8000-0000000000b0")  # Agent Zaaheen's own workspace in these tests
EVA_ROW = WorkspaceRow(workspace_id=EVA, slug="eva", name="Eva")
BOARD_ROW = WorkspaceRow(workspace_id=BOARD, slug="agentzaaheen", name="Agent Zaaheen")


@pytest.fixture(autouse=True)
def _workspaces(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT, EVA, ZAAHEEN, BOARD))


class Clock:
    """A clock the test moves."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def control(db: Database, clock: Clock) -> ChatControl:
    return ChatControl(db, TENANT, BOARD, clock)


async def _ask(chats: ChatControl, workspace: WorkspaceRow | None, n: int) -> tuple[UUID, int, bool]:
    kept = await chats.keep(
        workspace, f"question {n}", f"answer {n}", model_id="gemma-local", from_sample=False
    )
    return kept.conversation_id, kept.turn, kept.started


def test_turns_join_the_conversation_until_it_sits_idle_for_three_hours(test_db: ScratchDb) -> None:
    async def body() -> None:
        clock = Clock(NOW)
        async with open_db(test_db.app_url) as db:
            chats = control(db, clock)
            first, turn, started = await _ask(chats, EVA_ROW, 1)
            assert (turn, started) == (1, True)
            clock.at = NOW + IDLE_AFTER - timedelta(seconds=1)
            again, turn, started = await _ask(chats, EVA_ROW, 2)
            assert (again, turn, started) == (first, 2, False)
            # Idle is counted from the last question, not the first.
            clock.at += IDLE_AFTER - timedelta(seconds=1)
            assert (await _ask(chats, EVA_ROW, 3))[:2] == (first, 3)
            clock.at += IDLE_AFTER
            later, turn, started = await _ask(chats, EVA_ROW, 4)
            assert later != first
            assert (turn, started) == (1, True)
            going = await chats.going_on(EVA_ROW)
            assert going is not None
            assert going.conversation_id == later
            assert [t.question for t in going.turns] == ["question 4"]

    run(body())


def test_going_on_is_none_once_idle_and_turns_come_back_oldest_first(test_db: ScratchDb) -> None:
    async def body() -> None:
        clock = Clock(NOW)
        async with open_db(test_db.app_url) as db:
            chats = control(db, clock)
            assert await chats.going_on(EVA_ROW) is None
            for n in range(1, 4):
                await _ask(chats, EVA_ROW, n)
            going = await chats.going_on(EVA_ROW)
            assert going is not None
            assert [(t.turn, t.question, t.answer) for t in going.turns] == [
                (1, "question 1", "answer 1"),
                (2, "question 2", "answer 2"),
                (3, "question 3", "answer 3"),
            ]
            assert going.turns[0].model_id == "gemma-local"
            clock.at = NOW + IDLE_AFTER
            assert await chats.going_on(EVA_ROW) is None

    run(body())


def test_each_workspace_and_the_board_keep_their_own_conversations(test_db: ScratchDb) -> None:
    async def body() -> None:
        clock = Clock(NOW)
        async with open_db(test_db.app_url) as db:
            chats = control(db, clock)
            eva, _, _ = await _ask(chats, EVA_ROW, 1)
            board, _, board_started = await _ask(chats, None, 2)
            project, _, project_started = await _ask(chats, BOARD_ROW, 3)
            # Agent Zaaheen's own project chat and the Board's share a workspace, never a conversation.
            assert len({eva, board, project}) == 3
            assert board_started
            assert project_started
            board_now = await chats.going_on(None)
            project_now = await chats.going_on(BOARD_ROW)
            assert board_now is not None
            assert project_now is not None
            assert [t.question for t in board_now.turns] == ["question 2"]
            assert [t.question for t in project_now.turns] == ["question 3"]
            assert (
                await chats.going_on(WorkspaceRow(workspace_id=ZAAHEEN, slug="zaaheen", name="Zaaheen"))
                is None
            )

    run(body())


def test_the_wall_hides_another_workspaces_chats(test_db: ScratchDb) -> None:
    async def body() -> int:
        async with open_db(test_db.app_url) as db:
            await _ask(control(db, Clock(NOW)), EVA_ROW, 1)
        conn = await connect_as(test_db.app_url, TENANT, ZAAHEEN)
        try:
            seen: int = await conn.fetchval("SELECT count(*) FROM agentzaaheen.chat_turns")
            seen += await conn.fetchval("SELECT count(*) FROM agentzaaheen.chat_conversations")
            return seen
        finally:
            await conn.close()

    assert run(body()) == 0


def test_a_turn_cannot_be_written_into_another_workspace(test_db: ScratchDb) -> None:
    async def body() -> None:
        conn = await connect_as(test_db.app_url, TENANT, ZAAHEEN)
        try:
            await conn.execute(
                "INSERT INTO agentzaaheen.chat_conversations"
                " (tenant_id, project_id, conversation_id, for_board, started_at, last_at)"
                " VALUES ($1, $2, gen_random_uuid(), false, $3, $3)",
                TENANT,
                EVA,
                NOW,
            )
        finally:
            await conn.close()

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        run(body())


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE agentzaaheen.chat_turns SET answer = 'changed'",
        "DELETE FROM agentzaaheen.chat_turns",
        "DELETE FROM agentzaaheen.chat_conversations",
        "UPDATE agentzaaheen.chat_conversations SET cleared_for_learning = true",
    ],
)
def test_the_app_can_never_change_or_delete_a_chat(test_db: ScratchDb, statement: str) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await _ask(control(db, Clock(NOW)), EVA_ROW, 1)
        conn = await connect_as(test_db.app_url, TENANT, EVA)
        try:
            await conn.execute(statement)
        finally:
            await conn.close()

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        run(body())


def test_text_too_long_for_a_record_is_refused(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            rows = ChatRows(db.scoped(TENANT, EVA))
            await rows.add(
                "q", "a" * 4001, model_id="gemma-local", from_sample=False, at=NOW, idle_after=IDLE_AFTER
            )

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.REJECTED


def test_a_failed_turn_leaves_no_empty_conversation(test_db: ScratchDb) -> None:
    async def body() -> int:
        async with open_db(test_db.app_url) as db:
            rows = ChatRows(db.scoped(TENANT, EVA))
            with pytest.raises(StoreError):
                await rows.add(
                    "q", "a" * 4001, model_id="gemma-local", from_sample=False, at=NOW, idle_after=IDLE_AFTER
                )
        conn = await connect_as(test_db.app_url, TENANT, EVA)
        try:
            count: int = await conn.fetchval("SELECT count(*) FROM agentzaaheen.chat_conversations")
            return count
        finally:
            await conn.close()

    assert run(body()) == 0


def test_chats_unused_for_twelve_months_are_removed_and_only_those(test_db: ScratchDb) -> None:
    async def body() -> None:
        clock = Clock(NOW - KEEP_FOR - timedelta(days=1))
        async with open_db(test_db.app_url) as db:
            chats = control(db, clock)
            old_eva, _, _ = await _ask(chats, EVA_ROW, 1)
            await _ask(chats, EVA_ROW, 2)
            await _ask(chats, None, 3)  # the Board's, kept in Agent Zaaheen's workspace
            clock.at = NOW - KEEP_FOR + timedelta(days=1)  # under 12 months old at NOW: stays
            kept_zaaheen, _, _ = await _ask(
                chats, WorkspaceRow(workspace_id=ZAAHEEN, slug="zaaheen", name="Z"), 4
            )
            clock.at = NOW
            fresh_eva, _, _ = await _ask(chats, EVA_ROW, 5)
            gone = await chats.forget_old(
                [EVA_ROW, WorkspaceRow(workspace_id=ZAAHEEN, slug="zaaheen", name="Zaaheen")]
            )
            assert gone == 2  # Eva's old conversation and the Board's
            assert await chats.forget_old([EVA_ROW]) == 0
            going = await chats.going_on(EVA_ROW)
            assert going is not None
            assert going.conversation_id == fresh_eva != old_eva
        conn = await asyncpg.connect(test_db.admin_url)
        try:
            left = await conn.fetch(
                "SELECT project_id, conversation_id FROM agentzaaheen.chat_conversations ORDER BY project_id"
            )
            turns = await conn.fetchval("SELECT count(*) FROM agentzaaheen.chat_turns")
        finally:
            await conn.close()
        assert {(r["project_id"], r["conversation_id"]) for r in left} == {
            (EVA, fresh_eva),
            (ZAAHEEN, kept_zaaheen),
        }
        assert turns == 2

    run(body())

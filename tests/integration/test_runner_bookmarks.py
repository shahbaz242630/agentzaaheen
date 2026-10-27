"""Bookmarks against a real Postgres: handed to the agent, moved only with what it found (F11 slice 4).

The rules: an agent gets its bookmarks on its run ticket; they move in the
same save as the run's end and its findings, and only when the run ended well
with every finding kept. A failed run, a held-back finding or a save that
fails leaves them where they were. Bookmarks stay behind the workspace wall
and go when a workspace is deleted for good.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agentzaaheen.core.registry.roles import RoleLibrary, load_roles
from agentzaaheen.core.runner import AgentResult, FindingDraft, RunCode, RunReport
from agentzaaheen.core.sdk import RunOutcome
from agentzaaheen.core.store import MAX_BOOKMARKS, Bookmarks, Database, Records, StoreError, StoreErrorCode
from tests.integration.conftest import ScratchDb, add_workspaces, open_db, run
from tests.integration.test_runner import (
    EVA,
    LEAKED,
    NOW,
    TENANT,
    ZAAHEEN,
    FakeAgent,
    _ready,
    _refuse,
    checked_runner,
    found,
    leaky,
    runner,
)
from tests.integration.test_store_records import run_record
from tests.unit.test_registry_roles import pack


@pytest.fixture(autouse=True)
def _workspaces(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT, EVA, ZAAHEEN))


@pytest.fixture
def library(tmp_path: Path) -> RoleLibrary:
    pack(tmp_path, "monitoring")
    return load_roles(tmp_path)


def moving(bookmarks: Mapping[str, str], *findings: FindingDraft, crash: bool = False) -> FakeAgent:
    """An agent that says where it got to."""
    result = AgentResult(findings=findings, bookmarks=dict(bookmarks))
    return FakeAgent(answer=result, crash=crash)


async def _marks(db: Database, agent: UUID, workspace: UUID = EVA) -> dict[str, str]:
    return await Bookmarks(db.scoped(TENANT, workspace)).of(agent)


def test_bookmarks_move_with_the_runs_end_and_come_back_on_the_next_ticket(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    second = moving({"uptime.state": "up"})

    async def body() -> tuple[RunReport, dict[str, str], RunReport, UUID | None]:
        async with open_db(test_db.app_url) as db:
            agent = await _ready(db, library)
            first = await runner(db, library).run(EVA, agent, moving({"uptime.state": "down:x"}, found()))
            moved = await _marks(db, agent)
            again = await runner(db, library).run(EVA, agent, second)
            async with db.scoped(TENANT, EVA).connection() as conn:
                by = await conn.fetchval(
                    "SELECT run_id FROM agentzaaheen.bookmarks WHERE agent_id = $1", agent
                )
            return first, moved, again, by

    first, moved, again, by = run(body())
    assert (first.outcome, first.bookmarks) == (RunOutcome.OK, 1)
    assert moved == {"uptime.state": "down:x"}
    assert dict(second.tickets[0].bookmarks) == {"uptime.state": "down:x"}  # handed over at the start
    assert (again.outcome, again.bookmarks) == (RunOutcome.NOTHING_FOUND, 1)
    assert by == again.run_id  # the run that last moved it


def test_a_first_run_gets_no_bookmarks(test_db: ScratchDb, library: RoleLibrary) -> None:
    agent_code = moving({})

    async def body() -> RunReport:
        async with open_db(test_db.app_url) as db:
            return await runner(db, library).run(EVA, await _ready(db, library), agent_code)

    assert run(body()).bookmarks == 0
    assert dict(agent_code.tickets[0].bookmarks) == {}


def test_a_run_that_failed_leaves_the_bookmark_where_it_was(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def body() -> tuple[RunReport, dict[str, str]]:
        async with open_db(test_db.app_url) as db:
            agent = await _ready(db, library)
            await runner(db, library).run(EVA, agent, moving({"uptime.state": "up"}))
            failed = await runner(db, library).run(EVA, agent, moving({"uptime.state": "down:y"}, crash=True))
            return failed, await _marks(db, agent)

    failed, marks = run(body())
    assert (failed.outcome, failed.code, failed.bookmarks) == (RunOutcome.COULD_NOT_RUN, RunCode.CRASHED, 0)
    assert marks == {"uptime.state": "up"}


def test_a_held_back_finding_keeps_the_bookmark_so_the_next_run_sees_it_again(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    async def body() -> tuple[RunReport, dict[str, str]]:
        async with open_db(test_db.app_url) as db:
            agent = await _ready(db, library)
            agent_code = moving({"uptime.state": "down:z"}, found(), leaky(f"token={LEAKED}"))
            report = await checked_runner(db, library).run(EVA, agent, agent_code)
            return report, await _marks(db, agent)

    report, marks = run(body())
    assert (report.code, report.held_back, report.bookmarks) == (RunCode.LEAK_HELD_BACK, 1, 0)
    assert marks == {}


def test_an_end_that_cannot_be_saved_moves_no_bookmark(
    test_db: ScratchDb, library: RoleLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body() -> tuple[RunReport, dict[str, str]]:
        async with open_db(test_db.app_url) as db:
            agent = await _ready(db, library)
            with monkeypatch.context() as patched:
                patched.setattr(Records, "end_run", _refuse)
                report = await runner(db, library).run(EVA, agent, moving({"uptime.state": "up"}))
            return report, await _marks(db, agent)

    report, marks = run(body())
    assert (report.code, report.bookmarks) == (RunCode.END_NOT_WRITTEN, 0)
    assert marks == {}


def test_a_save_that_fails_halfway_keeps_neither_the_findings_nor_the_bookmark(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    """One transaction: the run, its findings, then its bookmarks. Any part refused undoes all of it."""

    async def body() -> tuple[RunReport, dict[str, str]]:
        async with open_db(test_db.app_url) as db:
            agent = await _ready(db, library)
            # Past the model's own check (built unchecked), the table refuses it: the whole save fails.
            bad = FakeAgent(
                answer=AgentResult.model_construct(findings=(found(),), bookmarks={"uptime.state": "x" * 257})
            )
            report = await runner(db, library).run(EVA, agent, bad)
            async with db.scoped(TENANT, EVA).connection() as conn:
                findings = await conn.fetchval("SELECT count(*) FROM agentzaaheen.findings")
                runs = await conn.fetchval("SELECT count(*) FROM agentzaaheen.runs")
            assert (findings, runs) == (0, 0)
            return report, await _marks(db, agent)

    report, marks = run(body())
    assert (report.code, report.bookmarks) == (RunCode.END_NOT_WRITTEN, 0)
    assert marks == {}


def test_an_unreadable_bookmark_is_a_run_that_did_not_run(
    test_db: ScratchDb, library: RoleLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_code = moving({})

    async def body() -> RunReport:
        async with open_db(test_db.app_url) as db:
            agent = await _ready(db, library)
            monkeypatch.setattr(Bookmarks, "of", _refuse)
            return await runner(db, library).run(EVA, agent, agent_code)

    report = run(body())
    assert (report.outcome, report.code) == (RunOutcome.DENIED, RunCode.BOOKMARKS_UNREADABLE)
    assert agent_code.tickets == []


@pytest.mark.parametrize(
    "bookmarks",
    [{"Uptime": "up"}, {"uptime.state": ""}, {"uptime.state": "x" * 257}, {f"b{n}": "1" for n in range(21)}],
)
def test_an_agent_cannot_name_a_bad_bookmark(bookmarks: dict[str, str]) -> None:
    assert MAX_BOOKMARKS == 20
    with pytest.raises(ValidationError):
        AgentResult(bookmarks=bookmarks)


def test_bookmarks_stay_behind_the_workspace_wall(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def body() -> tuple[dict[str, str], StoreErrorCode]:
        async with open_db(test_db.app_url) as db:
            agent = await _ready(db, library)
            await runner(db, library).run(EVA, agent, moving({"uptime.state": "up"}))
            elsewhere = await _marks(db, agent, ZAAHEEN)
            async with db.scoped(TENANT, ZAAHEEN).connection() as conn:
                run_id = await conn.fetchval("SELECT gen_random_uuid()")
            try:
                async with db.scoped(TENANT, ZAAHEEN).write("a bookmark") as conn:
                    await conn.execute(
                        "INSERT INTO agentzaaheen.bookmarks VALUES ($1, $2, $3, 'a', 'b', $4, now())",
                        TENANT,
                        EVA,
                        agent,
                        run_id,
                    )
            except StoreError as exc:
                return elsewhere, exc.code
            raise AssertionError("another workspace's bookmark was written")

    elsewhere, code = run(body())
    assert elsewhere == {}
    assert code is not None


def test_the_app_cannot_delete_a_bookmark(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def body() -> StoreErrorCode:
        async with open_db(test_db.app_url) as db:
            agent = await _ready(db, library)
            await runner(db, library).run(EVA, agent, moving({"uptime.state": "up"}))
            try:
                async with db.scoped(TENANT, EVA).write("a bookmark") as conn:
                    await conn.execute("DELETE FROM agentzaaheen.bookmarks")
            except StoreError as exc:
                return exc.code
            raise AssertionError("the app deleted a bookmark")

    assert run(body()) is not None


@pytest.mark.parametrize(
    ("agent", "bookmarks", "moved", "said"),
    [
        ("monitoring", {"a": "b"}, True, "agent id"),  # only an agent run (its id) has bookmarks
        (None, {"a": "b"}, False, "when"),
        (None, {f"b{n}": "1" for n in range(21)}, True, "at most"),
    ],
)
def test_the_store_refuses_bookmarks_it_cannot_place(
    test_db: ScratchDb, agent: str | None, bookmarks: dict[str, str], moved: bool, said: str
) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            ran = run_record(tenant_id=TENANT, project_id=EVA, agent=agent or str(uuid4()))
            await Records(db.scoped(TENANT, EVA)).end_run(ran, [], bookmarks, moved_at=NOW if moved else None)

    with pytest.raises(ValueError, match=said):
        run(body())

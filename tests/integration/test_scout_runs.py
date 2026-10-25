"""The scout's runs against a real Postgres (F17 slice 5b-2, ADR-039).

One run at a time per workspace (the database's own rule); a run left by a
dashboard that stopped is closed as interrupted; how a run ends is all or
nothing; runs sit behind the workspace walls, cannot be added to an archived
workspace and go when it is deleted for good; every ask is audited.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
import pytest

from agentzaaheen.core.policy import STALE_AFTER, Role, ScoutControl, ScoutVerdict, WorkspaceControl
from agentzaaheen.core.policy.access import Channel
from agentzaaheen.core.store import (
    INTERRUPTED,
    AuditRows,
    Database,
    ScoutRuns,
    StoreError,
    StoreErrorCode,
    WorkspaceRow,
)
from tests.integration.conftest import ScratchDb, add_workspaces, connect_as, open_db, run
from tests.integration.test_policy_agents import ME

TENANT = UUID("00000000-0000-4000-8000-0000000000c1")
SHOP_ID = UUID("00000000-0000-4000-8000-0000000000c2")
CAFE_ID = UUID("00000000-0000-4000-8000-0000000000c3")
SHOP = WorkspaceRow(SHOP_ID, "shop", "Shop")
CAFE = WorkspaceRow(CAFE_ID, "cafe", "Cafe")
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
REPORT = {"summary": "A shop.", "roles": [], "not_reviewed": [], "suggested_providers": [], "notes": []}


@pytest.fixture(autouse=True)
def _workspaces(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT, SHOP_ID, CAFE_ID))


def scout(db: Database, at: datetime = NOW) -> ScoutControl:
    return ScoutControl(db, TENANT, lambda: at)


async def ask(
    db: Database, workspace: WorkspaceRow = SHOP, at: datetime = NOW
) -> tuple[ScoutVerdict, UUID | None]:
    return await scout(db, at).start(workspace, repo="owner/shop", model="local-gemma", **ME)


def test_a_run_moves_through_its_stages_and_ends_with_its_advice(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            verdict, run_id = await ask(db)
            assert verdict is ScoutVerdict.STARTED
            assert run_id is not None
            first = await scout(db).latest(SHOP)
            assert first is not None
            assert (first.state, first.stage, first.branch, first.started_by) == (
                "running", "reading", None, "user:chairman",
            )  # fmt: skip
            await scout(db).stage(SHOP, run_id, "thinking", branch="main")
            await scout(db).stage(SHOP, run_id, "checking")
            assert await scout(db).finish(SHOP, run_id, REPORT)
            done = await scout(db).latest(SHOP)
            assert done is not None
            assert (done.state, done.stage, done.branch, done.result, done.reason) == (
                "done", "finished", "main", REPORT, None,
            )  # fmt: skip
            assert not await scout(db).fail(SHOP, run_id, "scout.late")  # an ended run stays ended

    run(body())


def test_one_run_at_a_time_per_workspace_but_workspaces_are_separate(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            assert (await ask(db))[0] is ScoutVerdict.STARTED
            assert (await ask(db, at=NOW + timedelta(minutes=1))) == (ScoutVerdict.BUSY, None)
            assert (await ask(db, CAFE))[0] is ScoutVerdict.STARTED

    run(body())


def test_a_run_left_by_a_stopped_dashboard_is_closed_as_interrupted(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            _, old = await ask(db)
            later = NOW + STALE_AFTER + timedelta(seconds=1)
            verdict, new = await ask(db, at=later)
            assert verdict is ScoutVerdict.STARTED
            assert new != old
            async with db.scoped(TENANT, SHOP_ID).connection() as conn:
                row = await conn.fetchrow(
                    "SELECT state, reason, finished_at FROM agentzaaheen.scout_runs WHERE run_id = $1", old
                )
            assert row is not None
            assert dict(row) == {"state": "failed", "reason": INTERRUPTED, "finished_at": later}

    run(body())


def test_a_failed_run_keeps_its_reason(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            _, run_id = await ask(db)
            assert run_id is not None
            assert await scout(db).fail(SHOP, run_id, "scout.not_json")
            failed = await scout(db).latest(SHOP)
            assert failed is not None
            assert (failed.state, failed.reason, failed.result) == ("failed", "scout.not_json", None)

    run(body())


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE agentzaaheen.scout_runs SET state = 'done'",  # done with no advice
        "UPDATE agentzaaheen.scout_runs SET state = 'failed', stage = 'finished', finished_at ="
        " now()",  # no reason
        "UPDATE agentzaaheen.scout_runs SET reason = 'scout.x'",  # a reason while running
        "UPDATE agentzaaheen.scout_runs SET result = '[1]'::jsonb",  # advice that is not an object
        "UPDATE agentzaaheen.scout_runs SET stage = 'finished'",  # finished while running
        "UPDATE agentzaaheen.scout_runs"
        " SET reason = 'Not a code', state = 'failed', stage = 'finished', finished_at = now()",
    ],
)
def test_the_database_refuses_a_run_that_does_not_add_up(test_db: ScratchDb, change: str) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await ask(db)
            async with db.scoped(TENANT, SHOP_ID).connection() as conn:
                await conn.execute(change)

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.REJECTED


def test_runs_stay_behind_the_workspace_wall(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await ask(db)
        conn = await connect_as(test_db.app_url, TENANT, CAFE_ID)
        try:
            # No WHERE clause: the wall alone decides what is seen.
            assert await conn.fetchval("SELECT count(*) FROM agentzaaheen.scout_runs") == 0
        finally:
            await conn.close()
        async with open_db(test_db.app_url) as db:
            assert await ScoutRuns(db.scoped(TENANT, CAFE_ID)).latest() is None

    run(body())


def test_the_app_may_not_delete_runs(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await ask(db)
            async with db.scoped(TENANT, SHOP_ID).connection() as conn:
                await conn.execute("DELETE FROM agentzaaheen.scout_runs")

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.DENIED


def _hello(n: int) -> datetime:
    return NOW - timedelta(seconds=60 - n)


def test_an_archived_workspace_takes_no_new_run_and_deleting_it_removes_its_runs(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            _, run_id = await ask(db)
            assert run_id is not None
            await scout(db).fail(SHOP, run_id, "scout.not_json")
            removal = WorkspaceControl(db, TENANT, lambda: NOW)
            await removal.archive(SHOP_ID, **ME, step_up_at=_hello(1))
            with pytest.raises(StoreError):
                await ask(db)
            await removal.erase(SHOP_ID, **ME, step_up_at=_hello(2))
        admin = await asyncpg.connect(test_db.admin_url)
        try:
            left = await admin.fetchval(
                "SELECT count(*) FROM agentzaaheen.scout_runs WHERE project_id = $1", SHOP_ID
            )
        finally:
            await admin.close()
        assert left == 0

    run(body())


def test_every_ask_is_audited_first_hand_and_a_viewer_may_not_ask(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await ask(db)
            await ask(db, at=NOW + timedelta(minutes=1))
            viewer = await scout(db).start(SHOP, repo="owner/shop", model="local-gemma", role=Role.VIEWER,
                                           channel=Channel.DASHBOARD)  # fmt: skip
            assert viewer == (ScoutVerdict.NOT_ALLOWED, None)
            audit = await AuditRows(db.scoped(TENANT)).all_rows()
        asks = [(e.outcome, e.reason_code, e.project_id) for e in audit if e.action == "workspaces.scout"]
        assert asks == [
            ("allow", "scout.started", SHOP_ID),
            ("deny", "scout.busy", SHOP_ID),
            ("deny", "scout.not_allowed", SHOP_ID),
        ]

    run(body())


def test_a_run_for_another_workspace_s_id_cannot_be_moved_from_here(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            _, run_id = await ask(db, CAFE)
            assert run_id is not None
            # Through Shop's handle, Cafe's run is not there: nothing changes.
            assert not await ScoutRuns(db.scoped(TENANT, SHOP_ID)).finish(run_id, REPORT, at=NOW)
            cafe = await scout(db).latest(CAFE)
            assert cafe is not None
            assert cafe.state == "running"

    run(body())


def test_a_new_run_id_is_used_each_time(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            _, first = await ask(db)
            assert first is not None
            await scout(db).fail(SHOP, first, "scout.not_json")
            _, second = await ask(db, at=NOW + timedelta(minutes=1))
            assert second not in {None, first}
            assert isinstance(uuid4(), UUID)

    run(body())


def test_closing_a_run_that_already_ended_logs_that_nothing_needed_writing(
    test_db: ScratchDb, capsys: pytest.CaptureFixture[str]
) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            _verdict, run_id = await ask(db)
            assert run_id is not None
            assert await scout(db).finish(SHOP, run_id, REPORT)
            capsys.readouterr()  # only the late writes' lines
            assert not await scout(db).fail(SHOP, run_id, "scout.late")
            await scout(db).stage(SHOP, run_id, "checking")

    run(body())
    events = [json.loads(raw) for raw in capsys.readouterr().err.splitlines() if raw.startswith("{")]
    said = [(e["event"], e["record"]) for e in events if e.get("event", "").startswith("store_")]
    assert said == [
        ("store_unchanged", "a failed scout run"), ("store_unchanged", "a scout run's stage"),
    ]  # fmt: skip

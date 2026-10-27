"""The Monitoring agent through the real runner and database, as the dashboard runs it (F11 slice 4).

Each round wakes every switched-on Monitoring agent; a change in uptime is
written down once, as a finding saved with its bookmark; an agent switched
off is not woken; one workspace's trouble never stops the others.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest

from agentzaaheen.core.api.workspaces import WorkspaceDirectory
from agentzaaheen.core.policy import AgentControl
from agentzaaheen.core.registry.roles import RoleLibrary, load_roles
from agentzaaheen.core.runner import Runner
from agentzaaheen.core.sdk import Expect, SiteCheck, UptimeReport
from agentzaaheen.core.store import (
    AgentRow,
    AuditRows,
    Bookmarks,
    Database,
    StoreError,
    StoreErrorCode,
    WorkspaceRow,
)
from agentzaaheen.plugins.agents.monitoring import BOOKMARK, UNLISTED, UP
from tests.integration.conftest import ScratchDb, add_workspaces, open_db, run
from tests.integration.test_policy_agents import ME
from tests.unit.test_registry_roles import pack
from tools.dashboard_monitoring import MONITORING_TOOLS, ROLE, LaptopMonitoring

TENANT = UUID("00000000-0000-4000-8000-0000000000d1")
EVA = UUID("00000000-0000-4000-8000-0000000000d2")
ZAAHEEN = UUID("00000000-0000-4000-8000-0000000000d3")
NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
WORKSPACES = (WorkspaceRow(EVA, "eva", "Eva"), WorkspaceRow(ZAAHEEN, "zaaheen", "Zaaheen"))
SITE = "https://eva.example.com"


def checked(*, up: bool) -> UptimeReport:
    site = SiteCheck(
        url=SITE,
        expect=Expect.OK,
        up=up,
        status=200 if up else 503,
        latency_ms=90,
        reason="uptime.ok" if up else "uptime.server_error",
        checked_at=NOW,
    )
    return UptimeReport(sites=(site,), fetched_at=NOW)


@pytest.fixture(autouse=True)
def _workspaces(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT, EVA, ZAAHEEN))


@pytest.fixture
def library(tmp_path: Path) -> RoleLibrary:
    pack(tmp_path, "monitoring")
    return load_roles(tmp_path)


async def _give(db: Database, library: RoleLibrary, workspace: UUID, *, on: bool = True) -> UUID:
    rules = AgentControl(db, TENANT, library, lambda: NOW)
    await rules.give(workspace, ROLE, **ME)
    (view,) = (await rules.view(workspace)).agents
    agent = view.agent.agent_id
    await rules.set_workspace(workspace, enabled=True, cap_minor=100, **ME, step_up_at=NOW)
    await rules.set_agent(workspace, agent, enabled=on, cap_minor=1, **ME, step_up_at=NOW)
    return agent


class Rounds:
    """The dashboard's job over the real runner, with the uptime watch's newest checks set by the test."""

    def __init__(self, db: Database, library: RoleLibrary, *, listed: frozenset[str]) -> None:
        self.latest: dict[str, UptimeReport] = {}
        self.broken: set[UUID] = set()
        self._db = db

        async def runner() -> Runner:
            control = AgentControl(db, TENANT, library, lambda: NOW)
            return Runner(db, TENANT, control, MONITORING_TOOLS, lambda: NOW)

        async def agents(workspace: WorkspaceRow, role: str) -> Sequence[AgentRow]:
            if workspace.workspace_id in self.broken:
                raise StoreError(StoreErrorCode.UNAVAILABLE, "the database went away")
            return await AgentControl(db, TENANT, library, lambda: NOW).doing(workspace.workspace_id, role)

        self.job = LaptopMonitoring(
            runner=runner,
            agents=agents,
            directory=WorkspaceDirectory(WORKSPACES),
            latest=lambda: self.latest,
            listed=listed,
            clock=lambda: NOW,
            every_s=0.01,
        )

    async def findings(self, workspace: UUID = EVA) -> list[asyncpg.Record]:
        async with self._db.scoped(TENANT, workspace).connection() as conn:
            return await conn.fetch(
                "SELECT f.title, f.severity, f.trust, f.data_class, r.outcome FROM agentzaaheen.findings f"
                " JOIN agentzaaheen.runs r ON r.id = f.run_id ORDER BY r.started_at, f.title"
            )

    async def mark(self, agent: UUID, workspace: UUID = EVA) -> Mapping[str, str]:
        return await Bookmarks(self._db.scoped(TENANT, workspace)).of(agent)


def test_down_is_written_once_then_back_up_once(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def body() -> tuple[list[int], list[asyncpg.Record], Mapping[str, str]]:
        async with open_db(test_db.app_url) as db:
            agent = await _give(db, library, EVA)
            rounds = Rounds(db, library, listed=frozenset({"eva", "zaaheen"}))
            started = []
            for up in (True, False, False, True, True):
                rounds.latest = {"eva": checked(up=up)}
                started.append(await rounds.job.round())
            return started, await rounds.findings(), await rounds.mark(agent)

    started, findings, mark = run(body())
    assert started == [1, 1, 1, 1, 1]  # Eva's agent each round; Zaaheen has none
    assert [(f["title"], f["severity"], f["outcome"]) for f in findings] == [
        ("Eva: 1 of 1 addresses down", "P2", "ok"),
        ("Eva: all 1 addresses up again", "P3", "ok"),
    ]
    assert {(f["trust"], f["data_class"]) for f in findings} == {("untrusted", "internal")}
    assert mark == {BOOKMARK: UP}


def test_an_agent_switched_off_is_not_woken(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def body() -> tuple[int, int, int]:
        async with open_db(test_db.app_url) as db:
            await _give(db, library, EVA, on=False)
            rounds = Rounds(db, library, listed=frozenset({"eva"}))
            rounds.latest = {"eva": checked(up=False)}
            started = await rounds.job.round()
            async with db.scoped(TENANT, EVA).connection() as conn:
                runs = await conn.fetchval("SELECT count(*) FROM agentzaaheen.runs")
            skips = [r for r in await AuditRows(db.scoped(TENANT)).all_rows() if r.action == "run.start"]
            return started, runs, len(skips)

    assert run(body()) == (0, 0, 0)  # not even counted as skipped: it was never woken


def test_a_project_not_read_or_not_listed_is_said_never_up(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def body() -> tuple[list[asyncpg.Record], list[asyncpg.Record], Mapping[str, str]]:
        async with open_db(test_db.app_url) as db:
            await _give(db, library, EVA)
            zaaheen = await _give(db, library, ZAAHEEN)
            rounds = Rounds(db, library, listed=frozenset({"eva"}))  # Eva listed but not read yet
            await rounds.job.round()
            return (
                await rounds.findings(),
                await rounds.findings(ZAAHEEN),
                await rounds.mark(zaaheen, ZAAHEEN),
            )

    eva, zaaheen, mark = run(body())
    assert [f["title"] for f in eva] == ["Eva: uptime could not be read"]
    assert [f["title"] for f in zaaheen] == ["Zaaheen: no addresses to watch"]
    assert mark == {BOOKMARK: UNLISTED}


def test_one_workspace_that_cannot_be_read_never_stops_the_others(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    async def body() -> tuple[int, list[asyncpg.Record]]:
        async with open_db(test_db.app_url) as db:
            await _give(db, library, EVA)
            await _give(db, library, ZAAHEEN)
            rounds = Rounds(db, library, listed=frozenset({"eva", "zaaheen"}))
            rounds.broken = {EVA}
            rounds.latest = {"zaaheen": checked(up=False)}
            return await rounds.job.round(), await rounds.findings(ZAAHEEN)

    started, zaaheen = run(body())
    assert started == 1
    assert [f["title"] for f in zaaheen] == ["Zaaheen: 1 of 1 addresses down"]


def test_the_rounds_run_on_their_own_and_stop_when_closed(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def body() -> int:
        async with open_db(test_db.app_url) as db:
            await _give(db, library, EVA)
            rounds = Rounds(db, library, listed=frozenset({"eva"}))
            rounds.latest = {"eva": checked(up=True)}
            await rounds.job.start()
            await asyncio.sleep(0.5)
            await rounds.job.close()
            async with db.scoped(TENANT, EVA).connection() as conn:
                before = await conn.fetchval("SELECT count(*) FROM agentzaaheen.runs")
            await asyncio.sleep(0.1)
            async with db.scoped(TENANT, EVA).connection() as conn:
                after = await conn.fetchval("SELECT count(*) FROM agentzaaheen.runs")
            assert after == before
            return int(before)

    assert run(body()) >= 1

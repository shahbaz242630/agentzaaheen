"""The runner against a real Postgres: checks before a run, its start and end, runs that died (F11 slice 1).

Every agent here is a fake: it records the ticket it was given and returns
what the test says. The rules: a job that was not meant to run is not started
(no run row); any other refusal is a run that did not run (a ``denied`` row,
named in the summary); a started run is written down first, gets its frozen
role pack, must finish inside its time budget, and its end, findings and real
cost are saved together; a start that never ends is closed as interrupted,
with the cost of the Claude calls it made, so its caps still count it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn
from uuid import UUID, uuid4

import asyncpg
import pytest

from agentzaaheen.core.policy import AgentControl, AgentVerdict, Budget
from agentzaaheen.core.registry.roles import RoleLibrary, load_roles
from agentzaaheen.core.runner import (
    CLOSE_GRACE,
    AgentResult,
    FindingDraft,
    RunCode,
    Runner,
    RunReport,
    RunTicket,
)
from agentzaaheen.core.sdk import (
    DataClass,
    Evidence,
    FindingKind,
    RunOutcome,
    Severity,
    ToolLevel,
    ToolSpec,
    Trust,
)
from agentzaaheen.core.store import (
    RUN_INTERRUPTED,
    AgentRows,
    AuditRows,
    Database,
    KillSwitchRows,
    ModelCalls,
    Records,
    RoleVersionRows,
    RunStart,
    RunStarts,
    StoreError,
    StoreErrorCode,
    SwitchScope,
)
from tests.integration.conftest import ScratchDb, add_workspaces, open_db, run
from tests.integration.test_policy_agents import ME
from tests.integration.test_store_records import run_record
from tests.unit.test_registry_roles import pack

TENANT = UUID("00000000-0000-4000-8000-0000000000c1")
EVA = UUID("00000000-0000-4000-8000-0000000000c2")
ZAAHEEN = UUID("00000000-0000-4000-8000-0000000000c3")
NOW = datetime(2026, 10, 26, 18, 0, tzinfo=UTC)
CHAIRMAN = "user:chairman"
BUDGET = Budget(steps=10, tokens=10_000, seconds=30)
TOOLS = {
    "uptime.read": ToolSpec(
        name="uptime.read",
        level=ToolLevel.READ,
        output_data_class=DataClass.PUBLIC,
        description="Read uptime.",
    ),
    "email.draft": ToolSpec(
        name="email.draft",
        level=ToolLevel.DRAFT,
        output_data_class=DataClass.INTERNAL,
        target_kind="email",
        description="Draft an email.",
    ),
    **{
        f"read.t{n}": ToolSpec(
            name=f"read.t{n}",
            level=ToolLevel.READ,
            output_data_class=DataClass.PUBLIC,
            description="A read tool.",
        )
        for n in range(7)
    },
}


def _hello(n: int) -> datetime:
    return NOW - timedelta(seconds=60 - n)


def found(title: str = "Eva is down") -> FindingDraft:
    return FindingDraft(
        kind=FindingKind.INCIDENT,
        severity=Severity.P2,
        title=title,
        body="The address answered 503 three times.",
        evidence=(Evidence(metric_id="uptime.eva"),),
        trust=Trust.TRUSTED,
        data_class=DataClass.PUBLIC,
    )


@dataclass
class FakeAgent:
    """An agent that records its ticket and does what the test says."""

    role_id: str = "monitoring"
    tools: frozenset[str] = frozenset({"uptime.read"})
    budget: Budget = BUDGET
    findings: tuple[FindingDraft, ...] = ()
    sleep: float = 0.0
    crash: bool = False
    answer: object = None  # when set, returned instead of an AgentResult
    during: object = None  # an async callable run with the ticket, mid-run
    tickets: list[RunTicket] = field(default_factory=list)

    async def run(self, ticket: RunTicket) -> AgentResult:
        self.tickets.append(ticket)
        if self.during is not None:
            await self.during(ticket)  # type: ignore[operator]
        if self.sleep:
            await asyncio.sleep(self.sleep)
        if self.crash:
            raise RuntimeError("a bug in the agent")
        if self.answer is not None:
            return self.answer  # type: ignore[return-value]
        return AgentResult(findings=self.findings)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(autouse=True)
def _workspaces(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT, EVA, ZAAHEEN))


@pytest.fixture
def library(tmp_path: Path) -> RoleLibrary:
    pack(tmp_path, "monitoring")
    pack(tmp_path, "sales", permission="Draft")
    return load_roles(tmp_path)


async def _ready(db: Database, library: RoleLibrary, *, agent_cap: int | None = 500, on: bool = True) -> UUID:
    """Eva's monitoring agent, given; switched on with caps unless told otherwise."""
    rules = AgentControl(db, TENANT, library, lambda: NOW)
    assert await rules.give(EVA, "monitoring", **ME) is AgentVerdict.DONE
    (view,) = (await rules.view(EVA)).agents
    agent = view.agent.agent_id
    await rules.set_workspace(EVA, enabled=True, cap_minor=1000, **ME, step_up_at=_hello(1))
    if on:
        await rules.set_agent(EVA, agent, enabled=True, cap_minor=agent_cap, **ME, step_up_at=_hello(2))
    return agent


def runner(db: Database, library: RoleLibrary, clock: Clock | None = None) -> Runner:
    tick = clock or Clock()
    return Runner(db, TENANT, AgentControl(db, TENANT, library, tick), TOOLS, tick)


async def _runs(db: Database, workspace: UUID = EVA) -> list[asyncpg.Record]:
    async with db.scoped(TENANT, workspace).connection() as conn:
        return await conn.fetch(
            "SELECT id, agent, outcome, error_code, cost_minor_units, tokens_in FROM agentzaaheen.runs"
            " WHERE tenant_id = $1 AND project_id = $2 ORDER BY started_at, id",
            TENANT,
            workspace,
        )


async def _audit(db: Database) -> list[tuple[str, str]]:
    return [(r.action, r.reason_code) for r in await AuditRows(db.scoped(TENANT)).all_rows()]


def test_a_run_that_passes_every_check_is_written_down_first_and_saved_with_its_findings(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    agent_code = FakeAgent(findings=(found(), found("Eva is slow")))
    seen_start: list[int] = []

    async def count_starts(ticket: RunTicket) -> None:  # the start is saved before the agent works
        async with open_db(test_db.app_url) as other, other.scoped(TENANT, EVA).connection() as conn:
            seen_start.append(
                await conn.fetchval(
                    "SELECT count(*) FROM agentzaaheen.run_starts WHERE run_id = $1", ticket.run_id
                )
            )

    agent_code.during = count_starts

    async def body() -> tuple[
        RunReport, UUID, list[asyncpg.Record], int, dict[str, str], list[tuple[str, str]]
    ]:
        async with open_db(test_db.app_url) as db:
            agent = await _ready(db, library)
            report = await runner(db, library).run(EVA, agent, agent_code)
            assert report.run_id is not None
            async with db.scoped(TENANT, EVA).connection() as conn:
                findings = await conn.fetchval(
                    "SELECT count(*) FROM agentzaaheen.findings WHERE run_id = $1", report.run_id
                )
            stored = await RoleVersionRows(db.scoped(TENANT)).get("monitoring", agent_code.tickets[0].version)
            assert stored is not None
            return report, agent, await _runs(db), findings, stored.files, await _audit(db)

    report, agent, runs, findings, files, audit = run(body())
    assert (report.outcome, report.code, report.findings) == (RunOutcome.OK, None, 2)
    assert seen_start == [1]
    ((run_id, who, outcome, code, cost, _),) = runs
    assert (run_id, who, outcome, code, cost) == (report.run_id, str(agent), "ok", None, 0)
    assert findings == 2
    ticket = agent_code.tickets[0]
    assert (ticket.workspace_id, ticket.agent_id, ticket.role_id) == (EVA, agent, "monitoring")
    assert dict(ticket.pack) == files  # the frozen copy
    assert audit[-2:] == [("run.start", "run.ok"), ("run.end", "run.ok")]


def test_a_run_that_finds_nothing_says_nothing_found_never_did_not_run(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    async def body() -> RunReport:
        async with open_db(test_db.app_url) as db:
            return await runner(db, library).run(EVA, await _ready(db, library), FakeAgent())

    assert run(body()).outcome is RunOutcome.NOTHING_FOUND


@pytest.mark.parametrize(
    ("switch", "ref", "code"),
    [
        (SwitchScope.ALL, "", RunCode.STOP_ALL),
        (SwitchScope.PROJECT, str(EVA), RunCode.WORKSPACE_FROZEN),
        (SwitchScope.AGENT, "agent", RunCode.AGENT_PAUSED),
    ],
)
def test_a_stop_switch_means_the_run_is_not_started(
    test_db: ScratchDb, library: RoleLibrary, switch: SwitchScope, ref: str, code: RunCode
) -> None:
    agent_code = FakeAgent()

    async def body() -> tuple[RunReport, list[asyncpg.Record], list[tuple[str, str]]]:
        async with open_db(test_db.app_url) as db:
            agent = await _ready(db, library)
            await KillSwitchRows(db.scoped(TENANT)).set(
                switch,
                str(agent) if ref == "agent" else ref,
                engaged=True,
                changed_by=CHAIRMAN,
                changed_at=NOW,
            )
            report = await runner(db, library).run(EVA, agent, agent_code)
            return report, await _runs(db), await _audit(db)

    report, runs, audit = run(body())
    assert (report.started, report.code) == (False, code)
    assert runs == []
    assert agent_code.tickets == []
    assert audit[-1] == ("run.start", code.value)


def test_an_agent_switched_off_is_not_started(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def body() -> tuple[RunReport, list[asyncpg.Record]]:
        async with open_db(test_db.app_url) as db:
            agent = await _ready(db, library, on=False)
            return await runner(db, library).run(EVA, agent, FakeAgent()), await _runs(db)

    report, runs = run(body())
    assert (report.started, report.code, runs) == (False, "run.agent_off", [])


def test_a_used_up_cap_is_a_run_that_did_not_run_and_is_named(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    agent_code = FakeAgent()

    async def body() -> tuple[RunReport, list[tuple[str, str | None]]]:
        async with open_db(test_db.app_url) as db:
            agent = await _ready(db, library)
            spent = run_record(tenant_id=TENANT, project_id=EVA, agent=str(agent), cost_minor_units=500)
            await Records(db.scoped(TENANT, EVA)).add_run(spent)
            report = await runner(db, library).run(EVA, agent, agent_code)
            missed = await RunStarts(db.scoped(TENANT, EVA)).missed(
                NOW - timedelta(days=1), NOW + timedelta(1)
            )
            return report, [(m.role_id, m.error_code) for m in missed]

    report, missed = run(body())
    assert (report.outcome, report.code) == (RunOutcome.DENIED, "run.agent_cap_used")
    assert agent_code.tickets == []
    assert ("monitoring", "run.agent_cap_used") in missed


def test_no_cap_means_no_run(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def body() -> RunReport:
        async with open_db(test_db.app_url) as db:
            return await runner(db, library).run(EVA, await _ready(db, library, agent_cap=None), FakeAgent())

    report = run(body())
    assert (report.outcome, report.code) == (RunOutcome.DENIED, "run.no_agent_cap")


@pytest.mark.parametrize(
    ("agent_code", "code"),
    [
        (FakeAgent(role_id="sales"), RunCode.WRONG_ROLE),
        (FakeAgent(tools=frozenset(f"read.t{n}" for n in range(7))), RunCode.TOO_MANY_TOOLS),
        (FakeAgent(tools=frozenset({"web.fetch"})), RunCode.UNKNOWN_TOOL),
        (FakeAgent(tools=frozenset({"email.draft"})), RunCode.TOOL_ABOVE_PERMISSION),  # a Read role
    ],
)
def test_code_that_is_not_right_for_the_role_is_refused_before_it_runs(
    test_db: ScratchDb, library: RoleLibrary, agent_code: FakeAgent, code: RunCode
) -> None:
    async def body() -> RunReport:
        async with open_db(test_db.app_url) as db:
            return await runner(db, library).run(EVA, await _ready(db, library), agent_code)

    report = run(body())
    assert (report.outcome, report.code) == (RunOutcome.DENIED, code)
    assert agent_code.tickets == []


def test_six_tools_are_allowed(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def body() -> RunReport:
        async with open_db(test_db.app_url) as db:
            six = FakeAgent(tools=frozenset(f"read.t{n}" for n in range(6)))
            return await runner(db, library).run(EVA, await _ready(db, library), six)

    assert run(body()).outcome is RunOutcome.NOTHING_FOUND


def test_a_missing_frozen_pack_is_refused(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def body() -> RunReport:
        async with open_db(test_db.app_url) as db:
            agent = await _ready(db, library)
            row = await AgentRows(db.scoped(TENANT, EVA)).get(agent)
            assert row is not None
        # The test's admin removes the stored copy (the app cannot): the runner must not use the live pack.
        conn = await asyncpg.connect(test_db.admin_url)
        try:
            # The database refuses this itself (agents point at their copy); go round it to test the runner.
            await conn.execute("SET session_replication_role = replica")
            await conn.execute("DELETE FROM agentzaaheen.role_versions WHERE version = $1", row.version)
        finally:
            await conn.close()
        async with open_db(test_db.app_url) as db:
            return await runner(db, library).run(EVA, agent, FakeAgent())

    report = run(body())
    assert (report.outcome, report.code) == (RunOutcome.DENIED, RunCode.NO_FROZEN_PACK)


@pytest.mark.parametrize(
    ("agent_code", "outcome", "code"),
    [
        (FakeAgent(crash=True), RunOutcome.COULD_NOT_RUN, RunCode.CRASHED),
        (FakeAgent(answer="all fine"), RunOutcome.COULD_NOT_RUN, RunCode.BAD_RESULT),
        (FakeAgent(budget=Budget(steps=10, tokens=10_000, seconds=1), sleep=5), RunOutcome.BUDGET_EXCEEDED,
         RunCode.TIME_BUDGET),
    ],
)  # fmt: skip
def test_a_run_that_goes_wrong_still_ends_with_a_row_that_says_why(
    test_db: ScratchDb, library: RoleLibrary, agent_code: FakeAgent, outcome: RunOutcome, code: RunCode
) -> None:
    async def body() -> tuple[RunReport, list[asyncpg.Record]]:
        async with open_db(test_db.app_url) as db:
            report = await runner(db, library).run(EVA, await _ready(db, library), agent_code)
            return report, await _runs(db)

    report, runs = run(body())
    assert (report.outcome, report.code) == (outcome, code)
    assert [(r["outcome"], r["error_code"]) for r in runs] == [(outcome.value, code.value)]


def test_a_runs_cost_is_added_up_from_its_claude_calls_and_counts_against_its_cap(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    async def spend(ticket: RunTicket) -> None:
        async with open_db(test_db.app_url) as other:
            calls = ModelCalls(other.scoped(TENANT))
            await calls.add("heads", "claude-opus-5-5", 2_000_001, at=NOW, run_id=str(ticket.run_id))
            await calls.add("heads", "claude-opus-5-5", 999_999, at=NOW)  # another run's: not this one's

    async def body() -> tuple[RunReport, list[asyncpg.Record], RunReport]:
        async with open_db(test_db.app_url) as db:
            agent = await _ready(db, library, agent_cap=301)
            first = await runner(db, library).run(EVA, agent, FakeAgent(during=spend))
            second = await runner(db, library).run(EVA, agent, FakeAgent())
            return first, await _runs(db), second

    first, runs, second = run(body())
    assert first.outcome is RunOutcome.NOTHING_FOUND
    (cost,) = [r["cost_minor_units"] for r in runs if r["id"] == first.run_id]
    assert cost == 201  # USD 2.000001 rounds up to 201 cents, never down
    assert (second.outcome, second.code) == (RunOutcome.NOTHING_FOUND, None)  # 201 < 301


def test_a_run_that_died_halfway_is_closed_as_interrupted_with_its_cost(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    clock = Clock()

    async def body() -> tuple[list[asyncpg.Record], list[tuple[str, str | None]], RunReport]:
        async with open_db(test_db.app_url) as db:
            agent = await _ready(db, library)
            row = await AgentRows(db.scoped(TENANT, EVA)).get(agent)
            assert row is not None
            dead, alive = uuid4(), uuid4()
            starts = RunStarts(db.scoped(TENANT, EVA))
            # One run started and died (its deadline passed); one is still inside its time.
            await starts.open(
                RunStart(dead, agent, "monitoring", row.version, NOW, NOW + timedelta(minutes=1))
            )
            await starts.open(
                RunStart(alive, agent, "monitoring", row.version, NOW, NOW + timedelta(hours=1))
            )
            await ModelCalls(db.scoped(TENANT)).add(
                "heads", "claude-opus-5-5", 1_500_000, at=NOW, run_id=str(dead)
            )
            clock.now = NOW + timedelta(minutes=5)
            report = await runner(db, library, clock).run(
                EVA, agent, FakeAgent()
            )  # closes the dead one first
            missed = await starts.missed(NOW - timedelta(days=1), NOW + timedelta(days=1))
            runs = [r for r in await _runs(db) if r["id"] in {dead, alive}]
            return runs, [(m.role_id, m.error_code) for m in missed], report

    runs, missed, report = run(body())
    assert [(r["outcome"], r["error_code"], r["cost_minor_units"]) for r in runs] == [
        ("could_not_run", RUN_INTERRUPTED, 150)
    ]
    assert missed == [("monitoring", RUN_INTERRUPTED)]
    assert report.outcome is RunOutcome.NOTHING_FOUND


def test_one_workspace_never_closes_or_sees_anothers_runs(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def body() -> tuple[list[UUID], list[asyncpg.Record]]:
        async with open_db(test_db.app_url) as db:
            agent = await _ready(db, library)
            row = await AgentRows(db.scoped(TENANT, EVA)).get(agent)
            assert row is not None
            await RunStarts(db.scoped(TENANT, EVA)).open(
                RunStart(uuid4(), agent, "monitoring", row.version, NOW, NOW + timedelta(minutes=1))
            )
            closed = await RunStarts(db.scoped(TENANT, ZAAHEEN)).close_interrupted(NOW + timedelta(hours=1))
            return closed, await _runs(db, ZAAHEEN)

    assert run(body()) == ([], [])


def test_skipped_runs_are_read_back_by_role_for_the_daily_summary(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    # Chairman, 26 Oct 2026: a run skipped because it was not meant to run is counted in one summary line.
    async def body() -> tuple[list[tuple[str | None, str]], list[str | None], list[object]]:
        async with open_db(test_db.app_url) as db:
            agent = await _ready(db, library, on=False)
            for _ in range(2):
                await runner(db, library).run(EVA, agent, FakeAgent())
            rules = AgentControl(db, TENANT, library, lambda: NOW + timedelta(minutes=1))
            report = await rules.runs_report(EVA, NOW - timedelta(days=1))
            other = await rules.runs_report(ZAAHEEN, NOW - timedelta(days=1))
            return (
                [(s.role_id, s.reason) for s in report.skipped],
                [m.error_code for m in report.missed],
                [*other.skipped, *other.missed],
            )

    skipped, missed, other = run(body())
    assert skipped == [("monitoring", "run.agent_off"), ("monitoring", "run.agent_off")]
    assert missed == []
    assert other == []  # another workspace's skips are not this one's


def test_a_stop_switch_says_since_when_it_is_on(test_db: ScratchDb) -> None:
    async def body() -> list[tuple[SwitchScope, str, datetime | None]]:
        async with open_db(test_db.app_url) as db:
            switches = KillSwitchRows(db.scoped(TENANT))
            await switches.set(SwitchScope.ALL, "", engaged=True, changed_by=CHAIRMAN, changed_at=NOW)
            await switches.set(
                SwitchScope.PROJECT, str(EVA), engaged=False, changed_by=CHAIRMAN, changed_at=NOW
            )
            return [(s.scope, s.ref, s.since) for s in await switches.engaged()]

    assert run(body()) == [(SwitchScope.ALL, "", NOW)]


def _refuse(*_args: object, **_kwargs: object) -> NoReturn:
    raise StoreError(StoreErrorCode.UNAVAILABLE, "the database went away")


def test_a_start_that_cannot_be_written_means_the_agent_never_runs(
    test_db: ScratchDb, library: RoleLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_code = FakeAgent()

    async def body() -> tuple[RunReport, list[asyncpg.Record], list[tuple[str, str]]]:
        async with open_db(test_db.app_url) as db:
            agent = await _ready(db, library)
            monkeypatch.setattr(RunStarts, "open", _refuse)
            report = await runner(db, library).run(EVA, agent, agent_code)
            return report, await _runs(db), await _audit(db)

    report, runs, audit = run(body())
    assert (report.started, report.code) == (False, RunCode.START_NOT_WRITTEN)
    assert agent_code.tickets == []
    assert runs == []
    assert audit[-1] == ("run.start", RunCode.START_NOT_WRITTEN.value)


def test_an_end_that_cannot_be_saved_is_closed_as_interrupted_later(
    test_db: ScratchDb, library: RoleLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()

    async def body() -> tuple[RunReport, list[tuple[str | None, str | None]]]:
        async with open_db(test_db.app_url) as db:
            agent = await _ready(db, library)
            with monkeypatch.context() as patched:
                patched.setattr(Records, "end_run", _refuse)
                report = await runner(db, library, clock).run(EVA, agent, FakeAgent(findings=(found(),)))
            clock.now = NOW + timedelta(seconds=BUDGET.seconds) + CLOSE_GRACE + timedelta(seconds=1)
            rules = AgentControl(db, TENANT, library, clock)
            missed = (await rules.runs_report(EVA, NOW - timedelta(hours=1))).missed
            return report, [(m.role_id, m.error_code) for m in missed]

    report, missed = run(body())
    assert (report.outcome, report.code) == (RunOutcome.COULD_NOT_RUN, RunCode.END_NOT_WRITTEN)
    assert missed == [("monitoring", RUN_INTERRUPTED)]  # nothing is lost: it is named


def test_a_refusal_that_cannot_be_saved_is_still_refused(
    test_db: ScratchDb, library: RoleLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body() -> RunReport:
        async with open_db(test_db.app_url) as db:
            agent = await _ready(db, library, agent_cap=None)
            monkeypatch.setattr(Records, "end_run", _refuse)
            return await runner(db, library).run(EVA, agent, FakeAgent())

    report = run(body())
    assert (report.outcome, report.code) == (RunOutcome.DENIED, "run.no_agent_cap")


def test_a_skip_is_audited_even_when_its_role_cannot_be_read(
    test_db: ScratchDb, library: RoleLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body() -> tuple[RunReport, RunReport, list[tuple[str | None, str]]]:
        async with open_db(test_db.app_url) as db:
            agent = await _ready(db, library)
            unknown = await runner(db, library).run(EVA, uuid4(), FakeAgent())  # no such agent: no role
            await KillSwitchRows(db.scoped(TENANT)).set(
                SwitchScope.ALL, "", engaged=True, changed_by=CHAIRMAN, changed_at=NOW
            )
            with monkeypatch.context() as patched:
                patched.setattr(AgentRows, "get", _refuse)
                stopped = await runner(db, library).run(EVA, agent, FakeAgent())
            later = AgentControl(db, TENANT, library, lambda: NOW + timedelta(minutes=1))
            report = await later.runs_report(EVA, NOW - timedelta(hours=1))
            return unknown, stopped, [(s.role_id, s.reason) for s in report.skipped]

    unknown, stopped, skipped = run(body())
    assert (unknown.started, unknown.code) == (False, "run.no_agent")
    assert (stopped.started, stopped.code) == (False, RunCode.STOP_ALL)
    assert skipped == [(None, "run.no_agent"), (None, "run.stop_all")]

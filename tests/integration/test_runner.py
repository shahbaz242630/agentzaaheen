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

from agentzaaheen.core.llm_gateway import FixedNames, KnownNames
from agentzaaheen.core.policy import AgentControl, AgentVerdict, Budget
from agentzaaheen.core.registry.roles import RoleLibrary, load_roles
from agentzaaheen.core.runner import (
    CLOSE_GRACE,
    NEEDS_A_LOOK,
    AgentFailedError,
    AgentResult,
    CheckedModel,
    FindingDraft,
    RunCode,
    Runner,
    RunReport,
    RunTicket,
    ShapeRefusal,
    system_agent_id,
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


# ---- F11 slice 2: built-in jobs, the checked model, and known failures


class Count:
    """A shape: a whole number."""

    def check(self, answer: str) -> int:
        if not answer.isdigit():
            raise ShapeRefusal("test.not_a_number", "a whole number only.")
        return int(answer)


class Replies:
    """A model answering in turn; keeps how many times it was asked."""

    def __init__(self, *replies: str) -> None:
        self.replies = replies
        self.asked = 0

    async def chat(self, messages: object, *, max_tokens: int, temperature: float) -> str:
        self.asked += 1
        return self.replies[min(self.asked, len(self.replies)) - 1]


@dataclass
class FakeJob(FakeAgent):
    """A built-in job: like an agent, plus the fingerprint of its instructions."""

    role_id: str = "scout"
    tools: frozenset[str] = frozenset()
    version: str = "e" * 64
    asks: bool = True
    fail: str | None = None

    async def run(self, ticket: RunTicket) -> AgentResult:
        self.tickets.append(ticket)
        if self.fail is not None:
            raise AgentFailedError(self.fail)
        if self.asks:
            assert ticket.model is not None
            await ticket.model.answer([{"role": "user", "content": "How many?"}], Count(), max_tokens=5,
                                      temperature=0.0)  # fmt: skip
        return AgentResult(delivered=True)


def model_runner(db: Database, library: RoleLibrary, model: Replies | None) -> Runner:
    return Runner(db, TENANT, AgentControl(db, TENANT, library, lambda: NOW), TOOLS, lambda: NOW, model=model)


async def _steps(db: Database, run_id: UUID) -> tuple[str, str | None, int]:
    async with db.scoped(TENANT, EVA).connection() as conn:
        row = await conn.fetchrow(
            "SELECT outcome, error_code, steps FROM agentzaaheen.runs WHERE id = $1", run_id
        )
    assert row is not None
    return row["outcome"], row["error_code"], row["steps"]


def test_a_built_in_job_runs_through_the_runner_and_only_gets_the_checked_model(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    job, model = FakeJob(), Replies("seven", "7")

    async def body() -> tuple[RunReport, tuple[str, str | None, int], list[tuple[str, str | None]]]:
        async with open_db(test_db.app_url) as db:
            report = await model_runner(db, library, model).run_system(EVA, job)
            assert report.run_id is not None
            starts = RunStarts(db.scoped(TENANT, EVA))
            return (
                report,
                await _steps(db, report.run_id),
                [
                    (m.role_id, m.error_code)
                    for m in await starts.missed(NOW - timedelta(1), NOW + timedelta(1))
                ],
            )

    report, row, missed = run(body())
    assert (report.outcome, report.code) == (RunOutcome.OK, None)
    assert row == ("ok", None, 2)  # two tries: two steps
    assert missed == []
    ticket = job.tickets[0]
    assert isinstance(ticket.model, CheckedModel)
    assert ticket.agent_id == system_agent_id("scout")
    assert dict(ticket.pack) == {}
    assert not hasattr(ticket, "chat")  # no raw model on the ticket


def test_a_job_whose_answers_are_all_refused_needs_a_look_and_is_named(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    async def body() -> tuple[RunReport, tuple[str, str | None, int], list[tuple[str, str | None]]]:
        async with open_db(test_db.app_url) as db:
            report = await model_runner(db, library, Replies("seven")).run_system(EVA, FakeJob())
            assert report.run_id is not None
            starts = RunStarts(db.scoped(TENANT, EVA))
            return (
                report,
                await _steps(db, report.run_id),
                [
                    (m.role_id, m.error_code)
                    for m in await starts.missed(NOW - timedelta(1), NOW + timedelta(1))
                ],
            )

    report, row, missed = run(body())
    assert (report.outcome, report.code, report.detail) == (
        RunOutcome.COULD_NOT_RUN, NEEDS_A_LOOK, "test.not_a_number"
    )  # fmt: skip
    assert row == ("could_not_run", NEEDS_A_LOOK, 3)
    assert missed == [("scout", NEEDS_A_LOOK)]  # a built-in job is named by its role


@pytest.mark.parametrize(
    ("said", "saved"), [("source.unavailable", "source.unavailable"), ("Not a code", "run.crashed")]
)
def test_a_known_failure_keeps_its_code_and_anything_else_is_a_crash(
    test_db: ScratchDb, library: RoleLibrary, said: str, saved: str
) -> None:
    async def body() -> RunReport:
        async with open_db(test_db.app_url) as db:
            return await model_runner(db, library, None).run_system(EVA, FakeJob(fail=said))

    report = run(body())
    assert (report.outcome, report.code) == (RunOutcome.COULD_NOT_RUN, saved)


def test_a_built_in_job_only_reads_and_must_name_its_instructions(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    async def body() -> tuple[RunReport, RunReport]:
        async with open_db(test_db.app_url) as db:
            runner = model_runner(db, library, None)
            with pytest.raises(ValueError, match="SHA-256"):
                await runner.run_system(EVA, FakeJob(version="1.0.0", asks=False))
            drafting = await runner.run_system(EVA, FakeJob(tools=frozenset({"email.draft"}), asks=False))
            no_model = await runner.run_system(EVA, FakeJob())  # asks, but this runner has no model
            return drafting, no_model

    drafting, no_model = run(body())
    assert (drafting.outcome, drafting.code) == (RunOutcome.DENIED, RunCode.TOOL_ABOVE_PERMISSION)
    assert no_model.outcome is RunOutcome.COULD_NOT_RUN  # its ticket had no model to ask


def test_a_run_id_is_never_run_twice(test_db: ScratchDb, library: RoleLibrary) -> None:
    job = FakeJob(asks=False)

    async def body() -> tuple[RunReport, RunReport]:
        async with open_db(test_db.app_url) as db:
            runner, run_id = model_runner(db, library, None), uuid4()
            return await runner.run_system(EVA, job, run_id=run_id), await runner.run_system(
                EVA, job, run_id=run_id
            )

    first, second = run(body())
    assert first.outcome is RunOutcome.OK
    assert (second.started, second.code) == (False, RunCode.START_NOT_WRITTEN)
    assert len(job.tickets) == 1


# ---- before anything leaves (checkpoint 4, F11 slice 3)

LEAKED = "ghp_" + "Q" * 36  # built here: a token's shape, never a real one
NAMES = FixedNames(KnownNames.of(["Fatima Al Mansoori"], lambda text: text.encode()))


class BrokenNames:
    async def known_names(self) -> KnownNames:
        raise RuntimeError("the name list is unreadable")


def checked_runner(db: Database, library: RoleLibrary, names: object = NAMES) -> Runner:
    tick = Clock()
    return Runner(db, TENANT, AgentControl(db, TENANT, library, tick), TOOLS, tick, names=names)  # type: ignore[arg-type]


def leaky(
    body: str, *, data_class: DataClass = DataClass.PUBLIC, trust: Trust = Trust.TRUSTED
) -> FindingDraft:
    return found().model_copy(update={"body": body, "data_class": data_class, "trust": trust})


async def _findings(db: Database, run_id: UUID) -> list[asyncpg.Record]:
    async with db.scoped(TENANT, EVA).connection() as conn:
        return await conn.fetch(
            "SELECT title, body, data_class, trust FROM agentzaaheen.findings WHERE run_id = $1", run_id
        )


def test_a_finding_holding_a_secret_is_never_saved_and_the_run_says_so(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    agent_code = FakeAgent(findings=(found(), leaky(f"the log printed {LEAKED}")))

    async def body() -> tuple[RunReport, list[asyncpg.Record], list[asyncpg.Record], list[tuple[str, str]]]:
        async with open_db(test_db.app_url) as db:
            report = await checked_runner(db, library).run(EVA, await _ready(db, library), agent_code)
            assert report.run_id is not None
            return report, await _runs(db), await _findings(db, report.run_id), await _audit(db)

    report, runs, saved, audit = run(body())
    assert (report.outcome, report.code, report.findings, report.held_back) == (
        RunOutcome.OK,
        RunCode.LEAK_HELD_BACK,
        1,
        1,
    )
    ((_, _, outcome, code, _, _),) = runs
    assert (outcome, code) == ("ok", "run.leak_held_back")  # the run row names it
    assert [row["body"] for row in saved] == ["The address answered 503 three times."]
    assert LEAKED not in str(saved)
    assert ("run.leak_check", "run.leak_held_back") in audit


def test_a_run_whose_every_finding_was_held_back_did_not_deliver(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    async def body() -> RunReport:
        async with open_db(test_db.app_url) as db:
            agent = FakeAgent(findings=(leaky(f"token={LEAKED}"),))
            return await checked_runner(db, library).run(EVA, await _ready(db, library), agent)

    report = run(body())
    assert (report.outcome, report.code, report.findings, report.held_back) == (
        RunOutcome.COULD_NOT_RUN,
        RunCode.LEAK_HELD_BACK,
        0,
        1,
    )


@pytest.mark.parametrize("text", ["Reply to fatima@example.com today.", "Fatima Al Mansoori asked again."])
def test_customer_details_are_saved_as_customer_data_and_untrusted_stays_untrusted(
    test_db: ScratchDb, library: RoleLibrary, text: str
) -> None:
    async def body() -> tuple[RunReport, list[asyncpg.Record], list[tuple[str, str]]]:
        async with open_db(test_db.app_url) as db:
            agent = FakeAgent(findings=(leaky(text, data_class=DataClass.INTERNAL, trust=Trust.UNTRUSTED),))
            report = await checked_runner(db, library).run(EVA, await _ready(db, library), agent)
            assert report.run_id is not None
            return report, await _findings(db, report.run_id), await _audit(db)

    report, saved, audit = run(body())
    assert (report.outcome, report.code) == (RunOutcome.OK, None)
    ((_, _, data_class, trust),) = saved
    assert (data_class, trust) == ("customer-pii", "untrusted")
    assert ("run.leak_check", "run.ok") in audit


def test_an_unreadable_name_list_still_runs_the_other_nets(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def body() -> list[asyncpg.Record]:
        async with open_db(test_db.app_url) as db:
            agent = FakeAgent(findings=(leaky("Fatima Al Mansoori"), leaky("mail fatima@example.com")))
            report = await checked_runner(db, library, BrokenNames()).run(
                EVA, await _ready(db, library), agent
            )
            assert report.run_id is not None
            return await _findings(db, report.run_id)

    classes = sorted(row["data_class"] for row in run(body()))
    assert classes == ["customer-pii", "public"]

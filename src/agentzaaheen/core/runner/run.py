"""The runner: the one way an agent run starts, and the checks at its start and end (ADR-036, F11 slice 1).

:meth:`Runner.run` takes a job (a workspace and one of its agents, from the
job's own saved record, never from anything the run asks for) and the agent's
code. In order; the first failure wins:

**Before** (checkpoint 1):

1. Runs of this workspace that died halfway are closed as ``run.interrupted``.
2. Kill switches: stop everything, the workspace frozen, the agent paused.
3. The job's door (``AgentControl.may_run``): the agent and its workspace
   switched on, both caps set and not used up this month.
4. The agent's **frozen** role pack (the copy stored when the role was given).
5. The code is for that role; its tools are at most 6, every one known, none
   above the role's permission.
6. The start is written down (``run_starts``) before the agent does anything.

A job that was not meant to run (switched off, paused, stopped) is **not
started**: a log line and an audit entry, no run row. Any other refusal is a
run that **did not run** (``denied``): it gets a run row, so the morning
briefing names it.

**During:** the agent gets a :class:`~agentzaaheen.core.runner.agent.RunTicket`
and must finish inside its time budget.

**After** (checkpoint 5): the run's end and its findings are saved together;
its cost is added up from the Claude calls made for it (rounded up to the
cent), so its caps count it; one plain-English line and one audit entry.
A run that never reaches its end is closed later as interrupted (step 1, and
the daily summary), with the cost of the calls it made.

Checkpoints 2 (every tool call: ``policy.gate``), 3 (answer shape) and 4
(before anything leaves) are in other slices.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import ValidationError

from agentzaaheen.core.policy import (
    SKIPPED_REASONS,
    AgentControl,
    AgentPolicy,
    AuditDraft,
    AuditOutcome,
    KillSwitches,
    Ledger,
    RunRefusal,
    load_kill_switches,
)
from agentzaaheen.core.runner.agent import AgentResult, AgentWork, RunTicket, _mint
from agentzaaheen.core.sdk import (
    DataClass,
    Finding,
    RunOutcome,
    RunRecord,
    ToolLevel,
    ToolSpec,
    Trust,
    combine_trust,
    get_logger,
    max_data_class,
    say,
)
from agentzaaheen.core.store import (
    AgentRow,
    AgentRows,
    Database,
    Records,
    RoleVersionRows,
    RunStart,
    RunStarts,
    Scoped,
    StoreError,
)

CLOSE_GRACE = timedelta(seconds=60)  # after the time budget: time to save the end before "interrupted"
_ACTOR = "system:runner"


class RunCode(StrEnum):
    """Why a run ended the way it did. Safe to log, store and show."""

    STOP_ALL = "run.stop_all"
    WORKSPACE_FROZEN = "run.workspace_frozen"
    AGENT_PAUSED = "run.agent_paused"
    NO_FROZEN_PACK = "run.no_frozen_pack"
    WRONG_ROLE = "run.wrong_role"
    TOO_MANY_TOOLS = "run.too_many_tools"
    UNKNOWN_TOOL = "run.unknown_tool"
    TOOL_ABOVE_PERMISSION = "run.tool_above_permission"
    START_NOT_WRITTEN = "run.start_not_written"
    TIME_BUDGET = "run.time_budget"
    CRASHED = "run.crashed"
    BAD_RESULT = "run.bad_result"
    END_NOT_WRITTEN = "run.end_not_written"


# Not meant to run: said and audited (the daily summary counts them in one line), never a run row.
_NOT_STARTED = SKIPPED_REASONS


@dataclass(frozen=True)
class RunReport:
    """How one job went. ``run_id`` is set when a run row exists for it."""

    outcome: RunOutcome | None  # None: not started (it was not meant to run)
    code: str | None
    run_id: UUID | None = None
    findings: int = 0

    @property
    def started(self) -> bool:
        """True when the job counted as a run (it ran, or it did not run and is named)."""
        return self.outcome is not None


class Runner:
    """Starts every agent run of one tenant, through the checkpoints, and closes it."""

    def __init__(  # the database, whose, the job's door, the tools, the clock
        self,
        db: Database,
        tenant_id: UUID,
        agents: AgentControl,
        tools: Mapping[str, ToolSpec],
        clock: Callable[[], datetime],
    ) -> None:
        self._db = db
        self._tenant = tenant_id
        self._agents = agents
        self._tools = dict(tools)
        self._clock = clock
        self._ledger = Ledger(db, tenant_id)
        self._log = get_logger("agentzaaheen.runner")

    async def run(  # noqa: PLR0911  # each check can end the run
        self, workspace_id: UUID, agent_id: UUID, work: AgentWork
    ) -> RunReport:
        """Run ``work`` as agent ``agent_id`` of ``workspace_id``, if every check says yes."""
        await self._agents.close_interrupted(workspace_id)
        now = self._clock()
        scope = self._db.scoped(self._tenant, workspace_id)
        stopped = _stopped(await load_kill_switches(self._db, self._tenant), workspace_id, agent_id)
        if stopped is not None:
            return await self._not_started(workspace_id, agent_id, stopped, now)
        permit = await self._agents.may_run(workspace_id, agent_id)
        if permit.refusal is not None and permit.refusal in _NOT_STARTED:
            return await self._not_started(workspace_id, agent_id, permit.refusal, now)
        agent = permit.agent or await AgentRows(scope).get(agent_id)
        if agent is None:  # pragma: no cover  # may_run said the agent exists a moment ago
            return await self._not_started(workspace_id, agent_id, RunRefusal.NO_AGENT, now)
        if permit.refusal is not None:
            return await self._denied(scope, agent, permit.refusal, now)
        pack = await RoleVersionRows(self._db.scoped(self._tenant)).get(agent.role_id, agent.version)
        if pack is None:
            return await self._denied(scope, agent, RunCode.NO_FROZEN_PACK, now)
        refused = self._check_tools(work, agent, pack.permission)
        if refused is not None:
            return await self._denied(scope, agent, refused, now)
        run_id = uuid4()
        deadline = now + timedelta(seconds=work.budget.seconds) + CLOSE_GRACE
        start = RunStart(run_id, agent_id, agent.role_id, agent.version, now, deadline)
        try:
            opened = await RunStarts(scope).open(start)
        except StoreError as exc:
            say(
                self._log,
                "agent_run_not_started",
                agent_id=str(agent_id),
                reason=RunCode.START_NOT_WRITTEN.value,
                code=exc.code.value,
            )
            await self._audit(
                "run.start",
                AuditOutcome.DENY,
                RunCode.START_NOT_WRITTEN,
                workspace_id=workspace_id,
                agent_id=agent_id,
                now=now,
            )
            return RunReport(None, RunCode.START_NOT_WRITTEN)
        if not opened:  # pragma: no cover  # a fresh uuid4 is never already started
            return RunReport(None, RunCode.START_NOT_WRITTEN)
        say(
            self._log,
            "agent_run_started",
            role=agent.role_id,
            agent_id=str(agent_id),
            run_id=str(run_id),
            seconds=work.budget.seconds,
        )
        await self._audit(
            "run.start",
            AuditOutcome.ALLOW,
            None,
            workspace_id=workspace_id,
            agent_id=agent_id,
            now=now,
            run_id=run_id,
        )
        ticket = _mint(
            run_id=run_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            role_id=agent.role_id,
            version=agent.version,
            pack=pack.files,
            tools=work.tools,
            budget=work.budget,
            started_at=now,
        )
        outcome, code, result = await _work(work, ticket)
        return await self._end(scope, start, outcome, code, result)

    def _check_tools(self, work: AgentWork, agent: AgentRow, permission: str) -> RunCode | None:
        if work.role_id != agent.role_id:
            return RunCode.WRONG_ROLE
        ceiling = ToolLevel(permission)
        try:
            AgentPolicy(agent=agent.role_id, tools=work.tools, ceiling=ceiling, budget=work.budget)
        except ValidationError:
            return RunCode.TOO_MANY_TOOLS
        for name in sorted(work.tools):
            spec = self._tools.get(name)
            if spec is None or spec.name != name:
                return RunCode.UNKNOWN_TOOL
            if spec.level is ToolLevel.FORBIDDEN or spec.level.rank > ceiling.rank:
                return RunCode.TOOL_ABOVE_PERMISSION
        return None

    async def _end(  # the run, how it went, and what it found
        self,
        scope: Scoped,
        start: RunStart,
        outcome: RunOutcome,
        code: str | None,
        result: AgentResult | None,
    ) -> RunReport:
        now = self._clock()
        workspace = scope.require_workspace()
        drafts = result.findings if result is not None else ()
        findings = [
            Finding(tenant_id=self._tenant, project_id=workspace, run_id=start.run_id, **d.model_dump())
            for d in drafts
        ]
        trust, data_class = Trust.TRUSTED, DataClass.PUBLIC
        for finding in findings:
            trust = combine_trust(trust, finding.trust)
            data_class = max_data_class(data_class, finding.data_class)
        try:
            calls = await RunStarts(scope).calls(start.run_id)
            record = RunRecord(
                id=start.run_id,
                tenant_id=self._tenant,
                project_id=workspace,
                agent=str(start.agent_id),
                agent_version=start.version,
                prompt_hash=start.version,
                data_class_max=data_class,
                trust=trust,
                steps=0,
                tokens_in=calls.tokens_in,
                tokens_out=calls.tokens_out,
                cost_minor_units=calls.cost_minor,
                started_at=start.started_at,
                duration_ms=max(0, int((now - start.started_at).total_seconds() * 1000)),
                outcome=outcome,
                error_code=code,
            )
            await Records(scope).end_run(record, findings)
        except StoreError as exc:
            # The start stays without an end: it is closed as interrupted after its deadline.
            say(
                self._log,
                "agent_run_end_not_saved",
                role=start.role_id,
                run_id=str(start.run_id),
                code=exc.code.value,
            )
            await self._audit(
                "run.end",
                AuditOutcome.DENY,
                RunCode.END_NOT_WRITTEN,
                workspace_id=workspace,
                agent_id=start.agent_id,
                now=now,
                run_id=start.run_id,
            )
            return RunReport(RunOutcome.COULD_NOT_RUN, RunCode.END_NOT_WRITTEN, start.run_id)
        say(
            self._log,
            "agent_run_ended",
            role=start.role_id,
            run_id=str(start.run_id),
            outcome=outcome.value,
            reason=code or "none",
            findings=len(findings),
            cost_minor=calls.cost_minor,
        )
        ok = outcome in {RunOutcome.OK, RunOutcome.NOTHING_FOUND}
        await self._audit(
            "run.end",
            AuditOutcome.ALLOW if ok else AuditOutcome.DENY,
            code,
            workspace_id=workspace,
            agent_id=start.agent_id,
            now=now,
            run_id=start.run_id,
            extra={"outcome": outcome.value},
        )
        return RunReport(outcome, code, start.run_id, len(findings))

    async def _not_started(self, workspace_id: UUID, agent_id: UUID, code: str, now: datetime) -> RunReport:
        say(self._log, "agent_run_not_started", agent_id=str(agent_id), reason=code, code=code)
        role: dict[str, str] = {}
        try:  # the role names the skip in the daily summary; without one it is still counted
            agent = await AgentRows(self._db.scoped(self._tenant, workspace_id)).get(agent_id)
            if agent is not None:
                role["role"] = agent.role_id
        except StoreError:
            pass
        await self._audit(
            "run.start",
            AuditOutcome.DENY,
            code,
            workspace_id=workspace_id,
            agent_id=agent_id,
            now=now,
            extra=role,
        )
        return RunReport(None, code)

    async def _denied(self, scope: Scoped, agent: AgentRow, code: str, now: datetime) -> RunReport:
        """A run that did not run: its own row (outcome ``denied``), so the briefing names it."""
        workspace = scope.require_workspace()
        record = RunRecord(
            tenant_id=self._tenant,
            project_id=workspace,
            agent=str(agent.agent_id),
            agent_version=agent.version,
            prompt_hash=agent.version,
            data_class_max=DataClass.PUBLIC,
            trust=Trust.TRUSTED,
            steps=0,
            tokens_in=0,
            tokens_out=0,
            cost_minor_units=0,
            started_at=now,
            duration_ms=0,
            outcome=RunOutcome.DENIED,
            error_code=code,
        )
        say(
            self._log,
            "agent_run_denied",
            role=agent.role_id,
            agent_id=str(agent.agent_id),
            reason=code,
            code=code,
        )
        try:
            await Records(scope).end_run(record, ())
        except StoreError as exc:
            say(
                self._log,
                "agent_run_end_not_saved",
                role=agent.role_id,
                run_id=str(record.id),
                code=exc.code.value,
            )
        await self._audit(
            "run.start",
            AuditOutcome.DENY,
            code,
            workspace_id=workspace,
            agent_id=agent.agent_id,
            now=now,
            run_id=record.id,
        )
        return RunReport(RunOutcome.DENIED, code, record.id)

    async def _audit(  # noqa: PLR0913  # each is a separate audit field
        self,
        action: str,
        outcome: AuditOutcome,
        code: str | None,
        *,
        workspace_id: UUID,
        agent_id: UUID,
        now: datetime,
        run_id: UUID | None = None,
        extra: dict[str, str] | None = None,
    ) -> None:
        details: dict[str, str | int | bool | None] = {"agent_id": str(agent_id), **(extra or {})}
        if run_id is not None:
            details["run_id"] = str(run_id)
        await self._ledger.record(
            AuditDraft(
                at=now,
                actor=_ACTOR,
                action=action,
                outcome=outcome,
                reason_code=code or "run.ok",
                correlation_id=run_id or uuid4(),
                project_id=workspace_id,
                target_ref=f"agent:{agent_id}",
                details=details,
            )
        )


async def _work(work: AgentWork, ticket: RunTicket) -> tuple[RunOutcome, str | None, AgentResult | None]:
    """Run the agent inside its time budget. Whatever goes wrong becomes an outcome, never a crash."""
    log = get_logger("agentzaaheen.runner")
    try:
        async with asyncio.timeout(work.budget.seconds):
            result = await work.run(ticket)
    except TimeoutError:
        return RunOutcome.BUDGET_EXCEEDED, RunCode.TIME_BUDGET, None
    except Exception as exc:  # noqa: BLE001  # an agent's bug ends its run, never the runner
        say(log, "agent_run_crashed", role=work.role_id, kind=type(exc).__name__)
        return RunOutcome.COULD_NOT_RUN, RunCode.CRASHED, None
    if not isinstance(result, AgentResult):
        return RunOutcome.COULD_NOT_RUN, RunCode.BAD_RESULT, None
    return (RunOutcome.OK if result.findings else RunOutcome.NOTHING_FOUND), None, result


def _stopped(switches: KillSwitches, workspace_id: UUID, agent_id: UUID) -> RunCode | None:
    if switches.stop_all:
        return RunCode.STOP_ALL
    if workspace_id in switches.frozen_projects:
        return RunCode.WORKSPACE_FROZEN
    if str(agent_id) in switches.paused_agents:
        return RunCode.AGENT_PAUSED
    return None

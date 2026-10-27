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

Before the end is saved (checkpoint 4, :mod:`~agentzaaheen.core.runner.leak`):
a finding holding a secret is held back (named in the log, the audit chain and
the run row as ``run.leak_held_back``); one holding customer details it did not
declare is moved up to customer data. A run whose every finding was held back
did not deliver: ``could_not_run``.

Checkpoint 2 (every tool call) is ``policy.gate``; checkpoint 3 (answer shape) is
:mod:`~agentzaaheen.core.runner.shape`.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError

from agentzaaheen.core.llm_gateway import ChatModel, KnownNames, NameSource
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
from agentzaaheen.core.runner.agent import AgentResult, AgentWork, RunTicket, SystemWork, _mint
from agentzaaheen.core.runner.leak import check_findings
from agentzaaheen.core.runner.shape import AgentFailedError, CheckedModel, NeedsALookError
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
    AgentRows,
    Database,
    Records,
    RoleVersionRows,
    RunStart,
    RunStarts,
    Scoped,
    StoreError,
)

_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_CODE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")
_SYSTEM = UUID("6f1c7c9e-2b1d-5e57-9a54-7a7a3b0e5c11")  # the namespace of built-in jobs' fixed ids
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
    LEAK_HELD_BACK = "run.leak_held_back"  # a finding held a secret: not saved (checkpoint 4)


# Not meant to run: said and audited (the daily summary counts them in one line), never a run row.
_NOT_STARTED = SKIPPED_REASONS


@dataclass(frozen=True)
class RunReport:
    """How one job went. ``run_id`` is set when a run row exists for it."""

    outcome: RunOutcome | None  # None: not started (it was not meant to run)
    code: str | None
    run_id: UUID | None = None
    findings: int = 0
    detail: str | None = None  # needs a look: the last refused answer's code
    held_back: int = 0  # findings not saved because they held a secret (checkpoint 4)

    @property
    def started(self) -> bool:
        """True when the job counted as a run (it ran, or it did not run and is named)."""
        return self.outcome is not None


class Runner:
    """Starts every agent run of one tenant, through the checkpoints, and closes it."""

    def __init__(  # noqa: PLR0913  # the database, whose, the job's door, the tools, the clock, the model, names
        self,
        db: Database,
        tenant_id: UUID,
        agents: AgentControl,
        tools: Mapping[str, ToolSpec],
        clock: Callable[[], datetime],
        *,
        model: ChatModel | None = None,
        names: NameSource | None = None,
    ) -> None:
        self._db = db
        self._model = model  # agents never get it: only a CheckedModel that answers in their shape
        self._names = names  # our customers' names, for the leak check (none: names are not checked)
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
        who = _Who(agent_id, agent.role_id, agent.version)
        if permit.refusal is not None:
            return await self._denied(scope, who, permit.refusal, now)
        pack = await RoleVersionRows(self._db.scoped(self._tenant)).get(agent.role_id, agent.version)
        if pack is None:
            return await self._denied(scope, who, RunCode.NO_FROZEN_PACK, now)
        refused = self._check_tools(work, agent.role_id, pack.permission)
        if refused is not None:
            return await self._denied(scope, who, refused, now)
        return await self._start_and_work(scope, who, work, pack=pack.files, now=now, run_id=uuid4())

    async def run_system(
        self, workspace_id: UUID, work: SystemWork, *, run_id: UUID | None = None
    ) -> RunReport:
        """Run a built-in job (the scout) for ``workspace_id``: stop switches, tools, start, time, shape, end.

        It has no workspace agent, switch or cap (it is read-only and spends nothing); a stop switch still
        stops it. ``run_id`` lets the job's own record (the scout's run) share the run's id.
        """
        if not _FINGERPRINT.fullmatch(work.version):
            raise ValueError("a built-in job must name the SHA-256 of its instructions")
        await self._agents.close_interrupted(workspace_id)
        now = self._clock()
        scope = self._db.scoped(self._tenant, workspace_id)
        who = _Who(system_agent_id(work.role_id), work.role_id, work.version)
        stopped = _stopped(await load_kill_switches(self._db, self._tenant), workspace_id, who.agent_id)
        if stopped is not None:
            return await self._not_started(workspace_id, who.agent_id, stopped, now, role=work.role_id)
        refused = self._check_tools(work, work.role_id, "read")  # built-in jobs only ever read
        if refused is not None:
            return await self._denied(scope, who, refused, now)
        return await self._start_and_work(scope, who, work, pack={}, now=now, run_id=run_id or uuid4())

    async def _start_and_work(  # noqa: PLR0913  # where, who, its instructions, the code, when, which run
        self,
        scope: Scoped,
        who: _Who,
        work: AgentWork,
        *,
        pack: Mapping[str, str],
        now: datetime,
        run_id: UUID,
    ) -> RunReport:
        workspace_id = scope.require_workspace()
        deadline = now + timedelta(seconds=work.budget.seconds) + CLOSE_GRACE
        start = RunStart(run_id, who.agent_id, who.role_id, who.version, now, deadline)
        try:
            opened = await RunStarts(scope).open(start)
        except StoreError as exc:
            say(
                self._log,
                "agent_run_not_started",
                agent_id=str(who.agent_id),
                reason=RunCode.START_NOT_WRITTEN.value,
                code=exc.code.value,
            )
            await self._audit(
                "run.start",
                AuditOutcome.DENY,
                RunCode.START_NOT_WRITTEN,
                workspace_id=workspace_id,
                agent_id=who.agent_id,
                now=now,
            )
            return RunReport(None, RunCode.START_NOT_WRITTEN)
        if not opened:  # a run id given twice (a built-in job's own record): never run it again
            return RunReport(None, RunCode.START_NOT_WRITTEN)
        say(
            self._log,
            "agent_run_started",
            role=who.role_id,
            agent_id=str(who.agent_id),
            run_id=str(run_id),
            seconds=work.budget.seconds,
        )
        await self._audit(
            "run.start",
            AuditOutcome.ALLOW,
            None,
            workspace_id=workspace_id,
            agent_id=who.agent_id,
            now=now,
            run_id=run_id,
        )
        checked = (
            CheckedModel(self._model, role=who.role_id, run_id=str(run_id))
            if self._model is not None
            else None
        )
        ticket = _mint(
            run_id=run_id,
            workspace_id=workspace_id,
            agent_id=who.agent_id,
            role_id=who.role_id,
            version=who.version,
            pack=pack,
            tools=work.tools,
            budget=work.budget,
            started_at=now,
            model=checked,
        )
        worked = await _work(work, ticket)
        steps = checked.attempts if checked is not None else 0
        return await self._end(scope, start, worked, steps)

    def _check_tools(self, work: AgentWork, role_id: str, permission: str) -> RunCode | None:
        if work.role_id != role_id:
            return RunCode.WRONG_ROLE
        ceiling = ToolLevel(permission)
        try:
            AgentPolicy(agent=role_id, tools=work.tools, ceiling=ceiling, budget=work.budget)
        except ValidationError:
            return RunCode.TOO_MANY_TOOLS
        for name in sorted(work.tools):
            spec = self._tools.get(name)
            if spec is None or spec.name != name:
                return RunCode.UNKNOWN_TOOL
            if spec.level is ToolLevel.FORBIDDEN or spec.level.rank > ceiling.rank:
                return RunCode.TOOL_ABOVE_PERMISSION
        return None

    async def _end(self, scope: Scoped, start: RunStart, worked: _Worked, steps: int) -> RunReport:
        outcome, code, result = worked.outcome, worked.code, worked.result
        workspace = scope.require_workspace()
        checked = check_findings(result.findings if result is not None else (), await self._known_names())
        if checked.held_back or checked.retagged:
            await self._said_leaks(start, workspace, checked.held_back, checked.retagged)
        if checked.held_back:
            code = code or RunCode.LEAK_HELD_BACK
            if not checked.kept and not (result is not None and result.delivered):
                outcome = RunOutcome.COULD_NOT_RUN  # nothing it found could be kept: it did not deliver
        now = self._clock()
        findings = [
            Finding(tenant_id=self._tenant, project_id=workspace, run_id=start.run_id, **d.model_dump())
            for d in checked.kept
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
                steps=steps,
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
            return RunReport(
                RunOutcome.COULD_NOT_RUN,
                RunCode.END_NOT_WRITTEN,
                start.run_id,
                detail=worked.detail,
                held_back=checked.held_back,
            )
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
        return RunReport(outcome, code, start.run_id, len(findings), worked.detail, checked.held_back)

    async def _known_names(self) -> KnownNames | None:
        """Our customers' names for the leak check, or ``None`` (no list, or it could not be read)."""
        if self._names is None:
            return None
        try:
            return await self._names.known_names()
        except Exception:  # noqa: BLE001  # whatever failed, the other nets still run
            say(self._log, "leak_names_unreadable")
            return None

    async def _said_leaks(self, start: RunStart, workspace: UUID, held_back: int, retagged: int) -> None:
        """One line and one audit entry for what the leak check changed (kinds and counts, never the text)."""
        say(
            self._log,
            "agent_findings_leak_checked",
            role=start.role_id,
            run_id=str(start.run_id),
            held_back=held_back,
            retagged=retagged,
        )
        await self._audit(
            "run.leak_check",
            AuditOutcome.DENY if held_back else AuditOutcome.ALLOW,
            RunCode.LEAK_HELD_BACK if held_back else None,
            workspace_id=workspace,
            agent_id=start.agent_id,
            now=self._clock(),
            run_id=start.run_id,
            extra={"held_back": str(held_back), "retagged": str(retagged)},
        )

    async def _not_started(  # where, who, why, when, and the role when already known
        self, workspace_id: UUID, agent_id: UUID, code: str, now: datetime, *, role: str | None = None
    ) -> RunReport:
        say(self._log, "agent_run_not_started", agent_id=str(agent_id), reason=code, code=code)
        named: dict[str, str] = {"role": role} if role is not None else {}
        if role is None:
            try:  # the role names the skip in the daily summary; without one it is still counted
                agent = await AgentRows(self._db.scoped(self._tenant, workspace_id)).get(agent_id)
                if agent is not None:
                    named["role"] = agent.role_id
            except StoreError:
                pass
        await self._audit(
            "run.start",
            AuditOutcome.DENY,
            code,
            workspace_id=workspace_id,
            agent_id=agent_id,
            now=now,
            extra=named,
        )
        return RunReport(None, code)

    async def _denied(self, scope: Scoped, who: _Who, code: str, now: datetime) -> RunReport:
        """A run that did not run: its own row (outcome ``denied``), so the briefing names it."""
        workspace = scope.require_workspace()
        record = RunRecord(
            tenant_id=self._tenant,
            project_id=workspace,
            agent=str(who.agent_id),
            agent_version=who.version,
            prompt_hash=who.version,
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
            role=who.role_id,
            agent_id=str(who.agent_id),
            reason=code,
            code=code,
        )
        try:
            await Records(scope).end_run(record, ())
        except StoreError as exc:
            say(
                self._log,
                "agent_run_end_not_saved",
                role=who.role_id,
                run_id=str(record.id),
                code=exc.code.value,
            )
        await self._audit(
            "run.start",
            AuditOutcome.DENY,
            code,
            workspace_id=workspace,
            agent_id=who.agent_id,
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


@dataclass(frozen=True)
class _Who:
    """Who a run is for: the agent (or a built-in job's fixed id), its role, and what it runs."""

    agent_id: UUID
    role_id: str
    version: str


@dataclass(frozen=True)
class _Worked:
    """How the agent's part went. ``detail``: the last refused answer's code when it needs a look."""

    outcome: RunOutcome
    code: str | None
    result: AgentResult | None = None
    detail: str | None = None


def system_agent_id(role_id: str) -> UUID:
    """The fixed id a built-in job's runs are saved under (the same on every machine)."""
    return uuid5(_SYSTEM, role_id)


async def _work(work: AgentWork, ticket: RunTicket) -> _Worked:
    """Run the agent inside its time budget. Whatever goes wrong becomes an outcome, never a crash."""
    log = get_logger("agentzaaheen.runner")
    try:
        async with asyncio.timeout(work.budget.seconds):
            result = await work.run(ticket)
    except TimeoutError:
        return _Worked(RunOutcome.BUDGET_EXCEEDED, RunCode.TIME_BUDGET)
    except NeedsALookError as exc:
        return _Worked(RunOutcome.COULD_NOT_RUN, exc.code, detail=exc.last)
    except AgentFailedError as exc:  # a known reason, said by the agent (a source down, a store refused)
        code = exc.code if _CODE.fullmatch(exc.code) and len(exc.code) <= 64 else RunCode.CRASHED.value
        return _Worked(RunOutcome.COULD_NOT_RUN, code)
    except Exception as exc:  # noqa: BLE001  # an agent's bug ends its run, never the runner
        say(log, "agent_run_crashed", role=work.role_id, kind=type(exc).__name__)
        return _Worked(RunOutcome.COULD_NOT_RUN, RunCode.CRASHED)
    if not isinstance(result, AgentResult):
        return _Worked(RunOutcome.COULD_NOT_RUN, RunCode.BAD_RESULT)
    done = RunOutcome.OK if result.findings or result.delivered else RunOutcome.NOTHING_FOUND
    return _Worked(done, None, result)


def _stopped(switches: KillSwitches, workspace_id: UUID, agent_id: UUID) -> RunCode | None:
    if switches.stop_all:
        return RunCode.STOP_ALL
    if workspace_id in switches.frozen_projects:
        return RunCode.WORKSPACE_FROZEN
    if str(agent_id) in switches.paused_agents:
        return RunCode.AGENT_PAUSED
    return None

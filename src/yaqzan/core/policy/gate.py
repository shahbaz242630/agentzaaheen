"""The tool gate: decides, from server state only, whether an agent's tool call may go ahead.

Every tool call goes agent → gate → connector (SECURITY §6.2). The model's
output is only ever the *request*; the answer comes from declarations, policies
and state held by the server (SP-1). Anything unknown or failing is a denial
(SP-2). Checks run in a fixed order and the first failure wins:

    1. kill switches       5. taint + split-run rule (§6.3, §6.6)
    2. run identity        6. budgets (steps, tokens, wall clock)
    3. allowlist + tool    7. argument check (schema, server-side)
    4. permission levels   8. target belongs to this project

An allowed ``read`` call executes. An allowed ``draft`` or ``act`` call only
*proposes*: it becomes an Action row and nothing leaves until a human approves.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from yaqzan.core.policy.audit import AuditDraft, AuditOutcome
from yaqzan.core.sdk import (
    DataClass,
    ToolLevel,
    ToolSpec,
    Trust,
    combine_trust,
    contains_sensitive,
    max_data_class,
)

MAX_TOOLS_PER_AGENT = 6


class DenyReason(StrEnum):
    """Why the gate said no. Safe to log, audit and show."""

    STOP_ALL = "gate.stop_all"
    PROJECT_FROZEN = "gate.project_frozen"
    AGENT_PAUSED = "gate.agent_paused"
    RUN_MISMATCH = "gate.run_mismatch"
    TOOL_NOT_ALLOWED = "gate.tool_not_allowed"
    UNKNOWN_TOOL = "gate.unknown_tool"
    FORBIDDEN_TOOL = "gate.forbidden_tool"
    ABOVE_AGENT_CEILING = "gate.above_agent_ceiling"
    ABOVE_PROJECT_CEILING = "gate.above_project_ceiling"
    SPLIT_RUN_REQUIRED = "gate.split_run_required"
    STEP_BUDGET = "gate.step_budget_exceeded"
    TOKEN_BUDGET = "gate.token_budget_exceeded"  # noqa: S105  # nosec B105  # a reason code, not a secret
    TIME_BUDGET = "gate.time_budget_exceeded"
    BAD_ARGS = "gate.bad_args"
    MISSING_TARGET = "gate.missing_target"
    UNEXPECTED_TARGET = "gate.unexpected_target"
    WRONG_TARGET_KIND = "gate.wrong_target_kind"
    CLOCK_WENT_BACKWARDS = "gate.clock_went_backwards"
    TARGET_NOT_OWNED = "gate.target_not_owned"
    CHECK_FAILED = "gate.check_failed"


class Effect(StrEnum):
    """What happens to an allowed call."""

    EXECUTE = "execute"
    PROPOSE = "propose"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Budget(_Frozen):
    """Hard limits for one run (threat AG-4)."""

    steps: int = Field(ge=1, le=100)
    tokens: int = Field(ge=1)
    seconds: int = Field(ge=1)


class AgentPolicy(_Frozen):
    """What one agent may do, from its reviewed ``plugin.yaml``."""

    agent: str = Field(min_length=1, max_length=64)
    tools: frozenset[str] = Field(max_length=MAX_TOOLS_PER_AGENT)
    ceiling: ToolLevel
    budget: Budget

    @field_validator("ceiling")
    @classmethod
    def _ceiling_not_forbidden(cls, value: ToolLevel) -> ToolLevel:
        if value is ToolLevel.FORBIDDEN:
            raise ValueError("an agent ceiling cannot be 'forbidden'")
        return value


class ProjectPolicy(_Frozen):
    """The highest level any agent may reach on one project."""

    project_id: UUID
    ceiling: ToolLevel


class KillSwitches(_Frozen):
    """Pause one agent, freeze one project, or stop everything (SECURITY §6.8)."""

    stop_all: bool = False
    paused_agents: frozenset[str] = frozenset()
    frozen_projects: frozenset[UUID] = frozenset()


class RunState(_Frozen):
    """What the server knows about a run so far. Data class and taint only go up."""

    run_id: UUID
    agent: str
    project_id: UUID
    started_at: AwareDatetime
    steps_used: int = Field(default=0, ge=0)
    tokens_used: int = Field(default=0, ge=0)
    trust: Trust = Trust.TRUSTED
    data_class: DataClass = DataClass.PUBLIC

    def after_result(self, spec: ToolSpec, tokens: int) -> RunState:
        """Return the state after a tool result (and its tokens) entered the run."""
        return self.model_copy(
            update={
                "steps_used": self.steps_used + 1,
                "tokens_used": self.tokens_used + max(tokens, 0),
                "trust": combine_trust(self.trust, spec.result_trust),
                "data_class": max_data_class(self.data_class, spec.output_data_class),
            }
        )


class Target(_Frozen):
    """The object a call acts on. Resolved server-side; never trusted from the model."""

    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    ref: str = Field(min_length=1, max_length=256)


class ToolCall(_Frozen):
    """A model's request to use a tool. Everything in it is untrusted input."""

    run_id: UUID
    tool: str = Field(max_length=80)
    args: Mapping[str, object]
    target: Target | None = None


class Decision(_Frozen):
    """The gate's answer for one call."""

    allowed: bool
    tool: str
    effect: Effect | None = None
    reason: DenyReason | None = None
    detail: str | None = Field(default=None, max_length=200)
    proposal_tainted: bool = False


class GateChecks(Protocol):
    """Server-side lookups the gate needs. Any exception is treated as a denial."""

    def validate_args(self, spec: ToolSpec, args: Mapping[str, object]) -> str | None:
        """Return None if ``args`` match the tool's schema, else a short safe error."""

    def owns_target(self, project_id: UUID, target: Target) -> bool:
        """Return True only if ``target`` exists and belongs to ``project_id``."""


@dataclass(frozen=True, slots=True)
class _Context:
    call: ToolCall
    spec: ToolSpec | None
    agent: AgentPolicy
    project: ProjectPolicy
    switches: KillSwitches
    state: RunState
    checks: GateChecks
    now: datetime


_Denial = tuple[DenyReason, str | None]


def _check_switches(ctx: _Context) -> _Denial | None:
    if ctx.switches.stop_all:
        return DenyReason.STOP_ALL, None
    if ctx.state.project_id in ctx.switches.frozen_projects:
        return DenyReason.PROJECT_FROZEN, None
    if ctx.state.agent in ctx.switches.paused_agents:
        return DenyReason.AGENT_PAUSED, None
    return None


def _check_identity(ctx: _Context) -> _Denial | None:
    same_run = ctx.call.run_id == ctx.state.run_id
    same_agent = ctx.state.agent == ctx.agent.agent
    same_project = ctx.state.project_id == ctx.project.project_id
    if not (same_run and same_agent and same_project):
        return DenyReason.RUN_MISMATCH, None
    return None


def _check_tool(ctx: _Context) -> _Denial | None:
    if ctx.call.tool not in ctx.agent.tools:
        return DenyReason.TOOL_NOT_ALLOWED, None
    if ctx.spec is None or ctx.spec.name != ctx.call.tool:
        return DenyReason.UNKNOWN_TOOL, None
    return None


def _check_levels(ctx: _Context) -> _Denial | None:
    level = _spec(ctx).level
    if level is ToolLevel.FORBIDDEN:
        return DenyReason.FORBIDDEN_TOOL, None
    if level.rank > ctx.agent.ceiling.rank:
        return DenyReason.ABOVE_AGENT_CEILING, None
    if level.rank > ctx.project.ceiling.rank:
        return DenyReason.ABOVE_PROJECT_CEILING, None
    return None


def _check_split_run(ctx: _Context) -> _Denial | None:
    # The lethal trifecta: untrusted content + private data + a free-form way out.
    tainted = ctx.state.trust is Trust.UNTRUSTED
    private = ctx.state.data_class is not DataClass.PUBLIC
    if _spec(ctx).outbound and tainted and private:
        return DenyReason.SPLIT_RUN_REQUIRED, None
    return None


def _check_budget(ctx: _Context) -> _Denial | None:
    budget = ctx.agent.budget
    if ctx.state.steps_used >= budget.steps:
        return DenyReason.STEP_BUDGET, None
    if ctx.state.tokens_used >= budget.tokens:
        return DenyReason.TOKEN_BUDGET, None
    elapsed = (ctx.now - ctx.state.started_at).total_seconds()
    if elapsed < 0:
        return DenyReason.CLOCK_WENT_BACKWARDS, None
    if elapsed >= budget.seconds:
        return DenyReason.TIME_BUDGET, None
    return None


def _check_args(ctx: _Context) -> _Denial | None:
    error = ctx.checks.validate_args(_spec(ctx), ctx.call.args)
    if error is not None:
        return DenyReason.BAD_ARGS, error[:200]
    return None


def _check_target(ctx: _Context) -> _Denial | None:
    target, kind = ctx.call.target, _spec(ctx).target_kind
    if kind is None:
        return (DenyReason.UNEXPECTED_TARGET, None) if target is not None else None
    if target is None:
        return DenyReason.MISSING_TARGET, None
    if target.kind != kind:
        return DenyReason.WRONG_TARGET_KIND, None
    if ctx.checks.owns_target(ctx.state.project_id, target) is not True:
        return DenyReason.TARGET_NOT_OWNED, None
    return None


def _spec(ctx: _Context) -> ToolSpec:
    # Only reached after _check_tool has confirmed the spec exists.
    if ctx.spec is None:
        raise LookupError("tool spec missing after the tool check")
    return ctx.spec


_CHECKS: tuple[Callable[[_Context], _Denial | None], ...] = (
    _check_switches,
    _check_identity,
    _check_tool,
    _check_levels,
    _check_split_run,
    _check_budget,
    _check_args,
    _check_target,
)


def decide(  # noqa: PLR0913  # every input is a separate, independently sourced fact
    call: ToolCall,
    *,
    spec: ToolSpec | None,
    agent: AgentPolicy,
    project: ProjectPolicy,
    switches: KillSwitches,
    state: RunState,
    checks: GateChecks,
    now: datetime,
) -> Decision:
    """Return the gate's decision for ``call``. Never raises: failures become denials."""
    if now.tzinfo is None:
        return Decision(allowed=False, tool=call.tool, reason=DenyReason.CHECK_FAILED, detail="naive clock")
    ctx = _Context(call, spec, agent, project, switches, state, checks, now)
    for check in _CHECKS:
        try:
            denial = check(ctx)
        except Exception as exc:  # noqa: BLE001  # fail closed: any lookup failure is a denial
            denial = DenyReason.CHECK_FAILED, f"{check.__name__}: {type(exc).__name__}"
        if denial is not None:
            return Decision(allowed=False, tool=call.tool, reason=denial[0], detail=denial[1])
    level = _spec(ctx).level
    if level is ToolLevel.READ:
        return Decision(allowed=True, tool=call.tool, effect=Effect.EXECUTE)
    return Decision(
        allowed=True,
        tool=call.tool,
        effect=Effect.PROPOSE,
        proposal_tainted=state.trust is Trust.UNTRUSTED,
    )


def audit_draft(
    decision: Decision, call: ToolCall, state: RunState, *, now: datetime, correlation_id: UUID
) -> AuditDraft:
    """Turn a gate decision into an audit entry (every allow and deny is audited)."""
    target_ref: str | None = None
    if call.target is not None:
        ref = f"{call.target.kind}:{call.target.ref}"
        target_ref = ref if not contains_sensitive(ref) else f"{call.target.kind}:[withheld]"
    details: dict[str, str | int | bool | None] = {"tool": call.tool[:80], "run_id": str(state.run_id)}
    if decision.effect is not None:
        details["effect"] = decision.effect.value
    if decision.proposal_tainted:
        details["tainted"] = True
    return AuditDraft(
        at=now,
        actor=f"agent:{state.agent}",
        action="tool.call",
        outcome=AuditOutcome.ALLOW if decision.allowed else AuditOutcome.DENY,
        reason_code=decision.reason.value if decision.reason is not None else "gate.allowed",
        correlation_id=correlation_id,
        project_id=state.project_id,
        target_ref=target_ref,
        details=details,
    )

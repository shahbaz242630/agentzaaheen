"""Agents per workspace: giving roles, switches, spending caps, and the job's door (ADR-039, F17 slice 3).

Who may do what follows ``access.authorize``: giving a workspace an agent,
switching one off, pausing a workspace and lowering or removing a cap reduce
risk and need nothing more; switching on, resuming and raising (or first
setting) a cap need the Chairman's fresh passkey step-up. The database keeps
the same rules underneath (migration 0007). Every decision is audited.

:meth:`AgentControl.may_run` is the job's door: before any run, it opens the
handle for the job's own workspace (from the job's saved record, never from
anything the run asks for) and says yes only if the agent and its workspace
are on, both caps are set, and neither is used up this month. No cap means
no spending.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from yaqzan.core.policy.access import Channel, Operation, Role, authorize
from yaqzan.core.policy.audit import AuditDraft, AuditOutcome
from yaqzan.core.policy.ledger import Ledger
from yaqzan.core.registry.roles import RoleLibrary
from yaqzan.core.sdk import get_logger, say
from yaqzan.core.store import (
    AgentChange,
    AgentRow,
    AgentRows,
    Database,
    NewAgent,
    RoleVersion,
    RoleVersionRows,
    Scoped,
    WorkspaceSettings,
)

DEFAULT_MODEL = "local-gemma"  # the only choice until the model map (F10)
_ACTORS = {
    Role.CHAIRMAN: "user:chairman",
    Role.VIEWER: "user:viewer",
    Role.AI_OPERATOR: "system:ai-operator",
    Role.AGENT_AUDITOR: "system:agent-auditor",
}


class AgentVerdict(StrEnum):
    """What happened to one attempt. Safe to log, audit and show."""

    DONE = "agents.done"
    NOT_ALLOWED = "agents.not_allowed"
    NEEDS_PASSKEY = "agents.needs_passkey"  # a riskier change: sign this exact change with Windows Hello
    PASSKEY_FAILED = "agents.passkey_failed"
    UNKNOWN_ROLE = "agents.unknown_role"
    ALREADY_GIVEN = "agents.already_given"
    UNKNOWN_AGENT = "agents.unknown_agent"
    BAD_CAP = "agents.bad_cap"


class RunRefusal(StrEnum):
    """Why the job's door said no."""

    NO_AGENT = "run.no_agent"
    AGENT_OFF = "run.agent_off"
    WORKSPACE_OFF = "run.workspace_off"
    NO_WORKSPACE_CAP = "run.no_workspace_cap"
    NO_AGENT_CAP = "run.no_agent_cap"
    WORKSPACE_CAP_USED = "run.workspace_cap_used"
    AGENT_CAP_USED = "run.agent_cap_used"


@dataclass(frozen=True)
class RunPermit:
    """The job's door's answer. ``scope`` is the job's own workspace handle, given only on yes."""

    refusal: RunRefusal | None
    scope: Scoped | None = None
    agent: AgentRow | None = None

    @property
    def allowed(self) -> bool:
        """True when the agent may run now."""
        return self.refusal is None


@dataclass(frozen=True)
class AgentView:
    """One agent as the workspace page shows it."""

    agent: AgentRow
    title: str
    spent_minor: int
    newer_version: bool  # the role pack changed since this agent's frozen copy


@dataclass(frozen=True)
class WorkspaceAgents:
    """A workspace's switch, cap, spend this month, and its agents."""

    enabled: bool
    cap_minor: int | None
    spent_minor: int
    agents: tuple[AgentView, ...]


def month_start(now: datetime) -> datetime:
    """The first moment of ``now``'s month, in UTC: caps are monthly."""
    at = now.astimezone(UTC)
    return datetime(at.year, at.month, 1, tzinfo=UTC)


def _riskier_cap(old: int | None, new: int | None) -> bool:
    """A first cap, or a higher one, lets more be spent. No cap means nothing may be spent."""
    return new is not None and (old is None or new > old)


class AgentControl:
    """The rules for a tenant's agents, over the database, the role library and the audit log."""

    def __init__(
        self, db: Database, tenant_id: UUID, library: RoleLibrary, clock: Callable[[], datetime]
    ) -> None:
        self._db = db
        self._tenant = tenant_id
        self._library = library
        self._clock = clock
        self._ledger = Ledger(db, tenant_id)

    def _rows(self, workspace_id: UUID) -> AgentRows:
        # The workspace was chosen at a door (an API route's address, a job's saved record).
        return AgentRows(self._db.scoped(self._tenant, workspace_id))

    async def view(self, workspace_id: UUID) -> WorkspaceAgents:
        """The workspace's switch, cap and spend this month, and each agent with its own spend."""
        rows, since = self._rows(workspace_id), month_start(self._clock())
        settings = await rows.settings()
        views = []
        for agent in await rows.agents():
            pack = self._library.get(agent.role_id)
            views.append(
                AgentView(
                    agent=agent,
                    title=pack.title if pack else agent.role_id,
                    spent_minor=await rows.spent(since, agent.agent_id),
                    newer_version=pack is not None and pack.version != agent.version,
                )
            )
        return WorkspaceAgents(
            enabled=settings.enabled if settings else False,
            cap_minor=settings.cap_minor if settings else None,
            spent_minor=await rows.spent(since),
            agents=tuple(views),
        )

    async def give(self, workspace_id: UUID, role_id: str, *, role: Role, channel: Channel) -> AgentVerdict:
        """Give the workspace an agent for ``role_id``: a frozen copy of the pack, switched off, no cap."""
        now = self._clock()
        pack = self._library.get(role_id)
        if not authorize(role, Operation.ASSIGN_AGENT, channel, step_up_at=None, now=now).allowed:
            verdict = AgentVerdict.NOT_ALLOWED
        elif pack is None:
            verdict = AgentVerdict.UNKNOWN_ROLE
        else:
            await RoleVersionRows(self._db.scoped(self._tenant)).put(
                RoleVersion(pack.id, pack.version, pack.files, pack.permission, now)
            )
            added = await self._rows(workspace_id).add(
                NewAgent(uuid4(), pack.id, pack.version, DEFAULT_MODEL, by=_ACTORS[role], at=now)
            )
            verdict = AgentVerdict.DONE if added else AgentVerdict.ALREADY_GIVEN
        await self._audit("agents.give", verdict, role, workspace_id, now, target=f"role:{role_id}")
        return verdict

    async def set_agent(  # noqa: PLR0913  # which agent, its new switch and cap, who, from where, the step-up
        self,
        workspace_id: UUID,
        agent_id: UUID,
        *,
        enabled: bool,
        cap_minor: int | None,
        role: Role,
        channel: Channel,
        step_up_at: datetime | None = None,
    ) -> AgentVerdict:
        """Switch an agent on or off and set its cap. Going riskier needs ``step_up_at`` (a passkey)."""
        now, rows = self._clock(), self._rows(workspace_id)
        agent = await rows.get(agent_id)
        if cap_minor is not None and cap_minor < 0:
            verdict = AgentVerdict.BAD_CAP
        elif agent is None:
            verdict = AgentVerdict.UNKNOWN_AGENT
        else:
            riskier = (enabled and not agent.enabled) or _riskier_cap(agent.cap_minor, cap_minor)
            operation = Operation.RESUME if riskier else Operation.PAUSE_AGENT
            step = step_up_at if riskier else None
            # One Windows Hello approval covers one change: a reused one asks again.
            if riskier and (step is None or step == agent.step_up_at):
                verdict = AgentVerdict.NEEDS_PASSKEY
            elif not authorize(role, operation, channel, step_up_at=step, now=now).allowed:
                verdict = AgentVerdict.NOT_ALLOWED
            else:
                change = AgentChange(
                    enabled, cap_minor, _ACTORS[role], now, step if riskier else agent.step_up_at
                )
                changed = await rows.change(agent_id, change)
                verdict = AgentVerdict.DONE if changed else AgentVerdict.UNKNOWN_AGENT
        await self._audit("agents.set", verdict, role, workspace_id, now, target=f"agent:{agent_id}")
        return verdict

    async def set_workspace(  # noqa: PLR0913  # which workspace, its new switch and cap, who, from where, step-up
        self,
        workspace_id: UUID,
        *,
        enabled: bool,
        cap_minor: int | None,
        role: Role,
        channel: Channel,
        step_up_at: datetime | None = None,
    ) -> AgentVerdict:
        """Pause or resume a workspace and set its monthly cap. Going riskier needs ``step_up_at``."""
        now, rows = self._clock(), self._rows(workspace_id)
        if cap_minor is not None and cap_minor < 0:
            verdict = AgentVerdict.BAD_CAP
        else:
            before = await rows.settings()
            was_on, old_cap = (before.enabled, before.cap_minor) if before else (False, None)
            riskier = (enabled and not was_on) or _riskier_cap(old_cap, cap_minor)
            operation = Operation.RESUME if riskier else Operation.FREEZE_PROJECT
            kept = before.step_up_at if before else None
            step = step_up_at if riskier else None
            if riskier and (step is None or step == kept):
                verdict = AgentVerdict.NEEDS_PASSKEY
            elif not authorize(role, operation, channel, step_up_at=step, now=now).allowed:
                verdict = AgentVerdict.NOT_ALLOWED
            else:
                settings = WorkspaceSettings(
                    enabled, cap_minor, _ACTORS[role], now, step if riskier else kept
                )
                await rows.put_settings(settings)
                verdict = AgentVerdict.DONE
        await self._audit(
            "agents.workspace", verdict, role, workspace_id, now, target=f"workspace:{workspace_id}"
        )
        return verdict

    async def may_run(self, workspace_id: UUID, agent_id: UUID) -> RunPermit:
        """The job's door: may this agent of this workspace run now?

        ``workspace_id`` comes from the job's own saved record, never from the run.
        """
        rows, since = self._rows(workspace_id), month_start(self._clock())
        agent, settings = await rows.get(agent_id), await rows.settings()
        refusal: RunRefusal | None = None
        if agent is None:
            refusal = RunRefusal.NO_AGENT
        elif settings is None or not settings.enabled:
            refusal = RunRefusal.WORKSPACE_OFF
        elif not agent.enabled:
            refusal = RunRefusal.AGENT_OFF
        elif settings.cap_minor is None:
            refusal = RunRefusal.NO_WORKSPACE_CAP
        elif agent.cap_minor is None:
            refusal = RunRefusal.NO_AGENT_CAP
        elif await rows.spent(since) >= settings.cap_minor:
            refusal = RunRefusal.WORKSPACE_CAP_USED
        elif await rows.spent(since, agent_id) >= agent.cap_minor:
            refusal = RunRefusal.AGENT_CAP_USED
        if refusal is not None:
            say(
                get_logger("yaqzan.agents"), "agent_run_refused", agent_id=str(agent_id), reason=refusal.value
            )
            return RunPermit(refusal)
        return RunPermit(None, self._db.scoped(self._tenant, workspace_id), agent)

    async def _audit(  # noqa: PLR0913  # each is a separate audit field
        self,
        action: str,
        verdict: AgentVerdict,
        role: Role,
        workspace_id: UUID,
        now: datetime,
        *,
        target: str,
    ) -> None:
        say(
            get_logger("yaqzan.agents"),
            "agents_decided",
            action=action,
            actor=_ACTORS[role],
            verdict=verdict.value,
        )
        await self._ledger.record(
            AuditDraft(
                at=now,
                actor=_ACTORS[role],
                action=action,
                outcome=AuditOutcome.ALLOW if verdict is AgentVerdict.DONE else AuditOutcome.DENY,
                reason_code=verdict.value,
                correlation_id=uuid4(),
                project_id=workspace_id,
                target_ref=target,
                details={"verdict": verdict.value},
            )
        )

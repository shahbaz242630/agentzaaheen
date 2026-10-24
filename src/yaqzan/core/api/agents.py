"""The Agents tab: the role library, each workspace's agents, switches and caps (ADR-039, F17 slice 3).

Every route sits behind the workspace door (``/api/workspaces/{slug}/...``).
A change that lowers risk (switch off, pause, lower or remove a cap) is done
at once. A riskier one (switch on, resume, a first or higher cap) answers
``agents.needs_passkey``; the page then asks for options to sign *that exact
change* with Windows Hello (its values are in the challenge's purpose) and
sends the change again with the signed answer. A signature fits nothing else.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from yaqzan.core.api.workspaces import InWorkspace
from yaqzan.core.auth import PasskeyError
from yaqzan.core.policy import AgentControl, AgentVerdict, Channel, PasskeyApprovals, Role
from yaqzan.core.registry.roles import RoleLibrary
from yaqzan.core.store import StoreError, WorkspaceRow

_SLUG = r"^[a-z0-9]+(-[a-z0-9]+)*$"
MAX_CAP_MINOR = 100_000_000  # USD 1,000,000 a month: anything above is a typo


class _View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RoleChoice(_View):
    """One role in the library, as the "+ Add an agent" list shows it."""

    id: str = Field(pattern=_SLUG, max_length=64)
    title: str = Field(max_length=200)
    mission: str = Field(max_length=200)
    permission: Literal["read", "draft", "act"]


class AgentOut(_View):
    """One agent of a workspace."""

    id: UUID
    role_id: str = Field(pattern=_SLUG, max_length=64)
    title: str = Field(max_length=200)
    enabled: bool
    model: str
    cap_minor: int | None
    spent_minor: int = Field(ge=0)
    newer_version: bool


class WorkspaceAgentsOut(_View):
    """A workspace's switch, cap, spend this month and agents. ``switches``: changes are possible here."""

    enabled: bool
    cap_minor: int | None
    spent_minor: int = Field(ge=0)
    agents: tuple[AgentOut, ...]
    switches: bool


class GiveIn(_View):
    """Give the workspace an agent for this role."""

    role_id: str = Field(pattern=_SLUG, max_length=64)


class SwitchIn(_View):
    """The switch and cap wanted. ``cap_minor`` ``None`` removes the cap (then nothing may be spent)."""

    enabled: bool
    cap_minor: int | None = Field(default=None, ge=0, le=MAX_CAP_MINOR)


class SignedSwitchIn(SwitchIn):
    """The same change, with Windows Hello's signed answer when it is a riskier one."""

    credential: dict[str, Any] | None = None


class StepUpOptions(_View):
    """WebAuthn options for signing one exact change."""

    options: dict[str, Any]


def purpose(kind: Literal["agent", "workspace"], target: UUID, change: SwitchIn) -> str:
    """The challenge's purpose: names exactly this change, so its signature fits nothing else."""
    cap = "none" if change.cap_minor is None else str(change.cap_minor)
    return f"step:{kind}:{target}:{'on' if change.enabled else 'off'}:{cap}"


class AgentSwitches(Protocol):
    """What the Agents tab needs (``DashboardAgents`` in real use)."""

    def roles(self) -> tuple[RoleChoice, ...]:
        """The role library."""
        ...

    async def view(self, workspace: WorkspaceRow) -> WorkspaceAgentsOut:
        """The workspace's agents, switch and cap."""
        ...

    async def give(self, workspace: WorkspaceRow, role_id: str) -> AgentVerdict:
        """Give the workspace an agent (switched off, no cap)."""
        ...

    async def set_agent(
        self, workspace: WorkspaceRow, agent_id: UUID, change: SwitchIn, credential: dict[str, Any] | None
    ) -> AgentVerdict:
        """Change one agent's switch and cap."""
        ...

    async def set_workspace(
        self, workspace: WorkspaceRow, change: SwitchIn, credential: dict[str, Any] | None
    ) -> AgentVerdict:
        """Change the workspace's switch and cap."""
        ...

    async def options(self, step: str) -> dict[str, Any]:
        """Options for Windows Hello to sign the change named by ``step``."""
        ...


class DashboardAgents:
    """The Agents tab on this laptop: the dashboard acts as the Chairman (no login yet)."""

    def __init__(self, control: AgentControl, passkeys: PasskeyApprovals, library: RoleLibrary) -> None:
        self._control = control
        self._passkeys = passkeys
        self._library = library

    def roles(self) -> tuple[RoleChoice, ...]:
        """The role library, in name order."""
        return self.roles_of(self._library)

    @staticmethod
    def roles_of(library: RoleLibrary) -> tuple[RoleChoice, ...]:
        """A library as the "+ Add an agent" list shows it."""
        return tuple(
            RoleChoice(id=p.id, title=p.title, mission=p.mission, permission=p.permission)
            for p in library.packs
        )

    async def view(self, workspace: WorkspaceRow) -> WorkspaceAgentsOut:
        """The workspace's agents, switch, cap and spend this month."""
        seen = await self._control.view(workspace.workspace_id)
        agents = tuple(
            AgentOut(
                id=v.agent.agent_id,
                role_id=v.agent.role_id,
                title=v.title,
                enabled=v.agent.enabled,
                model=v.agent.model,
                cap_minor=v.agent.cap_minor,
                spent_minor=v.spent_minor,
                newer_version=v.newer_version,
            )
            for v in seen.agents
        )
        return WorkspaceAgentsOut(
            enabled=seen.enabled,
            cap_minor=seen.cap_minor,
            spent_minor=seen.spent_minor,
            agents=agents,
            switches=True,
        )

    async def give(self, workspace: WorkspaceRow, role_id: str) -> AgentVerdict:
        """Give the workspace an agent for ``role_id``."""
        return await self._control.give(
            workspace.workspace_id, role_id, role=Role.CHAIRMAN, channel=Channel.DASHBOARD
        )

    async def set_agent(
        self, workspace: WorkspaceRow, agent_id: UUID, change: SwitchIn, credential: dict[str, Any] | None
    ) -> AgentVerdict:
        """Change one agent; a signed answer must be for exactly this change."""
        step = None
        if credential is not None:
            step = await self._passkeys.step_up(purpose("agent", agent_id, change), credential)
            if step is None:
                return AgentVerdict.PASSKEY_FAILED
        return await self._control.set_agent(
            workspace.workspace_id,
            agent_id,
            enabled=change.enabled,
            cap_minor=change.cap_minor,
            role=Role.CHAIRMAN,
            channel=Channel.DASHBOARD,
            step_up_at=step,
        )

    async def set_workspace(
        self, workspace: WorkspaceRow, change: SwitchIn, credential: dict[str, Any] | None
    ) -> AgentVerdict:
        """Change the workspace's switch and cap; a signed answer must be for exactly this change."""
        step = None
        if credential is not None:
            step = await self._passkeys.step_up(
                purpose("workspace", workspace.workspace_id, change), credential
            )
            if step is None:
                return AgentVerdict.PASSKEY_FAILED
        return await self._control.set_workspace(
            workspace.workspace_id,
            enabled=change.enabled,
            cap_minor=change.cap_minor,
            role=Role.CHAIRMAN,
            channel=Channel.DASHBOARD,
            step_up_at=step,
        )

    async def options(self, step: str) -> dict[str, Any]:
        """Options for Windows Hello to sign the change named by ``step``."""
        return await self._passkeys.step_up_options(step)


def agent_routes(app: FastAPI, agents: AgentSwitches | None, refuse_foreign: Callable[..., Any]) -> None:
    """The Agents tab's routes, every one behind the workspace door."""

    def ready() -> AgentSwitches:
        if agents is None:
            raise HTTPException(status_code=503, detail="agents.read_only")
        return agents

    async def stored[T](call: Awaitable[T]) -> T:
        try:
            return await call
        except StoreError:
            raise HTTPException(status_code=503, detail="agents.store_unavailable") from None
        except PasskeyError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from None

    async def changed(workspace: WorkspaceRow, call: Awaitable[AgentVerdict]) -> WorkspaceAgentsOut:
        verdict = await stored(call)
        if verdict is not AgentVerdict.DONE:
            raise HTTPException(status_code=409, detail=verdict.value)
        return await stored(ready().view(workspace))

    @app.get("/api/roles")
    def list_roles() -> tuple[RoleChoice, ...]:
        return () if agents is None else agents.roles()

    @app.get("/api/workspaces/{slug}/agents")
    async def list_agents(workspace: InWorkspace) -> WorkspaceAgentsOut:
        if agents is None:
            return WorkspaceAgentsOut(enabled=False, cap_minor=None, spent_minor=0, agents=(), switches=False)
        return await stored(agents.view(workspace))

    @app.post("/api/workspaces/{slug}/agents", dependencies=[Depends(refuse_foreign)])
    async def give_agent(workspace: InWorkspace, given: GiveIn) -> WorkspaceAgentsOut:
        return await changed(workspace, ready().give(workspace, given.role_id))

    @app.post("/api/workspaces/{slug}/agents/{agent_id}", dependencies=[Depends(refuse_foreign)])
    async def set_agent(workspace: InWorkspace, agent_id: UUID, change: SignedSwitchIn) -> WorkspaceAgentsOut:
        wanted = SwitchIn(enabled=change.enabled, cap_minor=change.cap_minor)
        return await changed(workspace, ready().set_agent(workspace, agent_id, wanted, change.credential))

    @app.post("/api/workspaces/{slug}/agents/{agent_id}/options", dependencies=[Depends(refuse_foreign)])
    async def agent_options(workspace: InWorkspace, agent_id: UUID, change: SwitchIn) -> StepUpOptions:
        del workspace  # the door has checked the address; the purpose names the agent
        return StepUpOptions(options=await stored(ready().options(purpose("agent", agent_id, change))))

    @app.post("/api/workspaces/{slug}/settings", dependencies=[Depends(refuse_foreign)])
    async def set_workspace(workspace: InWorkspace, change: SignedSwitchIn) -> WorkspaceAgentsOut:
        wanted = SwitchIn(enabled=change.enabled, cap_minor=change.cap_minor)
        return await changed(workspace, ready().set_workspace(workspace, wanted, change.credential))

    @app.post("/api/workspaces/{slug}/settings/options", dependencies=[Depends(refuse_foreign)])
    async def workspace_options(workspace: InWorkspace, change: SwitchIn) -> StepUpOptions:
        return StepUpOptions(
            options=await stored(ready().options(purpose("workspace", workspace.workspace_id, change)))
        )

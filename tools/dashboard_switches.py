"""The dashboard's registry switches on this laptop, kept in the laptop database (ADR-030, F06 slice 2b).

``tools.dashboard --switches`` uses :func:`connect` before anything is read: it
sets up the laptop database (``tools.laptop_db``) and says how every item
stands, so a connector that is switched off is not read at all. While the
dashboard runs, :class:`LaptopSwitches` serves the Customize page's switches
and the passkey approvals (ADR-SEC-007), and tells the chat, before every
question, what it may use. Its connection pool opens on first use, inside the
web server's own event loop; the passkey's open challenges live with it, in
memory, for the life of the dashboard.

There is one tenant on the laptop: :data:`LAPTOP_TENANT`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import SecretStr

from agentzaaheen.core.api.agents import DashboardAgents, RoleChoice, SwitchIn, WorkspaceAgentsOut
from agentzaaheen.core.api.registry import DashboardSwitches, PasskeyStatus, RegistryPage, Use
from agentzaaheen.core.api.removal import DashboardRemoval
from agentzaaheen.core.api.workspaces import WorkspaceDirectory
from agentzaaheen.core.auth import Site
from agentzaaheen.core.llm_gateway import Inspect
from agentzaaheen.core.policy import (
    Added,
    AgentControl,
    AgentVerdict,
    AuditDraft,
    AuditOutcome,
    Channel,
    ClaudeKeyControl,
    ClaudeKeyStatus,
    ClaudeKeyVerdict,
    Ledger,
    LifecycleVerdict,
    PasskeyApprovals,
    RegistryControl,
    Removal,
    Role,
    ScoutControl,
    Standing,
    SummaryControl,
    Verdict,
    WorkspaceControl,
)
from agentzaaheen.core.registry import Kind, Registry
from agentzaaheen.core.registry.roles import RoleLibrary
from agentzaaheen.core.store import Database, DatabaseSettings, StoreError, WorkspaceRow, WorkspaceRows
from agentzaaheen.core.vault import Keyring
from tools import laptop_db
from tools.vault_key import CLAUDE_WORKSPACE, claude_slot

LAPTOP_TENANT = laptop_db.LAPTOP_TENANT
START_LAPTOP_DB = "uv run python -m tools.laptop_db up"

Prepare = Callable[[], Awaitable[SecretStr | None]]


@dataclass(frozen=True)
class Connected:
    """The laptop database as the dashboard starts: where it is, how every item stands, its workspaces."""

    url: SecretStr
    standing: Standing
    workspaces: tuple[WorkspaceRow, ...]


def connect(registry: Registry, prepare: Prepare = laptop_db.prepare) -> Connected | None:
    """The laptop database, how every item stands now, and its workspaces; ``None`` (after saying why)."""

    async def read() -> Connected | None:
        url = await prepare()
        if url is None:
            return None
        db = await Database.open(DatabaseSettings(dsn=url, min_size=1, max_size=1))
        try:
            standing = await RegistryControl(db, LAPTOP_TENANT, registry).standing()
            workspaces = await WorkspaceRows(db.scoped(LAPTOP_TENANT)).workspaces()
            return Connected(url, standing, tuple(workspaces))
        finally:
            await db.close()

    try:
        found = asyncio.run(read())
    except StoreError as exc:
        print(f"dashboard: the laptop database could not be used ({exc.code.value}).")
        return None
    if found is None:
        print(f"dashboard: the laptop database is not running. Start it with: {START_LAPTOP_DB}")
    return found


def off_connectors(standing: Standing, registry: Registry) -> frozenset[str]:
    """The connectors that are not on, so they are not read."""
    return frozenset(c.id for c in registry.connectors) - standing.on(Kind.CONNECTOR)


class LaptopSwitches:
    """The Customize page's switches and the chat's view of them, over one lazily opened pool."""

    def __init__(  # noqa: PLR0913  # the database, what exists, where it is used, the time, the passkey site
        self,
        url: SecretStr,
        registry: Registry,
        uses: Mapping[str, Mapping[str, Use]],
        clock: Callable[[], datetime],
        *,
        site: Site,
        enrol_code: str | None = None,
        library: RoleLibrary | None = None,
        keyring: Keyring | None = None,
    ) -> None:
        self._settings = DatabaseSettings(dsn=url, min_size=1, max_size=4)
        self._library = library or RoleLibrary((), ())
        self._agents: DashboardAgents | None = None
        self._removal: DashboardRemoval | None = None
        self._workspaces: WorkspaceControl | None = None
        self._scout: ScoutControl | None = None
        self._summaries: SummaryControl | None = None
        self._claude_key: ClaudeKeyControl | None = None
        self._keyring = keyring
        self._site = site
        self._enrol_code = enrol_code
        self._registry = registry
        self._uses = uses
        self._clock = clock
        self._switches: DashboardSwitches | None = None
        self._ledger: Ledger | None = None
        self._opening: asyncio.Lock | None = None
        self._db: Database | None = None

    async def _open(self) -> DashboardSwitches:
        if self._opening is None:
            self._opening = asyncio.Lock()
        async with self._opening:
            if self._switches is None:
                db = self._db = await Database.open(self._settings)
                self._ledger = Ledger(db, LAPTOP_TENANT)
                control = RegistryControl(db, LAPTOP_TENANT, self._registry)
                passkeys = PasskeyApprovals(
                    db, LAPTOP_TENANT, control, self._site, enrol_code=self._enrol_code, clock=self._clock
                )
                self._switches = DashboardSwitches(control, passkeys, self._registry, self._uses, self._clock)
                agents = AgentControl(db, LAPTOP_TENANT, self._library, self._clock)
                self._agents = DashboardAgents(agents, passkeys, self._library)
                self._workspaces = WorkspaceControl(db, LAPTOP_TENANT, self._clock)
                self._removal = DashboardRemoval(self._workspaces, passkeys)
                self._scout = ScoutControl(db, LAPTOP_TENANT, self._clock)
                self._summaries = SummaryControl(db, LAPTOP_TENANT, self._clock)
                self._claude_key = ClaudeKeyControl(
                    db,
                    LAPTOP_TENANT,
                    CLAUDE_WORKSPACE,
                    slot=claude_slot(db, self._keyring),
                    passkeys=passkeys,
                    clock=self._clock,
                )
        return self._switches

    async def _agents_now(self) -> DashboardAgents:
        await self._open()
        if self._agents is None:  # pragma: no cover  # _open always sets it
            raise RuntimeError("the laptop database is not open")
        return self._agents

    # ---- the Agents tab (ADR-039, F17 slice 3): the same passkey and challenges as Customize

    def roles(self) -> tuple[RoleChoice, ...]:
        """The role library."""
        return DashboardAgents.roles_of(self._library)

    async def view(self, workspace: WorkspaceRow) -> WorkspaceAgentsOut:
        """A workspace's agents, switch and cap."""
        return await (await self._agents_now()).view(workspace)

    async def give(self, workspace: WorkspaceRow, role_id: str) -> AgentVerdict:
        """Give a workspace an agent."""
        return await (await self._agents_now()).give(workspace, role_id)

    async def set_agent(
        self, workspace: WorkspaceRow, agent_id: UUID, change: SwitchIn, credential: dict[str, Any] | None
    ) -> AgentVerdict:
        """Change one agent."""
        return await (await self._agents_now()).set_agent(workspace, agent_id, change, credential)

    async def set_workspace(
        self, workspace: WorkspaceRow, change: SwitchIn, credential: dict[str, Any] | None
    ) -> AgentVerdict:
        """Change a workspace's switch and cap."""
        return await (await self._agents_now()).set_workspace(workspace, change, credential)

    async def options(self, step: str) -> dict[str, Any]:
        """Options to sign one exact change."""
        return await (await self._agents_now()).options(step)

    # ---- removing a project (ADR-039, F17 slice 4): the same passkey

    async def _removal_now(self) -> DashboardRemoval:
        await self._open()
        if self._removal is None:  # pragma: no cover  # _open always sets it
            raise RuntimeError("the laptop database is not open")
        return self._removal

    async def active(self) -> list[WorkspaceRow]:
        """The workspaces the dashboard serves."""
        return await (await self._removal_now()).active()

    # ---- the scout (ADR-039, F17 slice 5b-2): its runs, behind each workspace's wall

    async def scout_control(self) -> ScoutControl:
        """The scout's runs (opens the database on first use)."""
        await self._open()
        if self._scout is None:  # pragma: no cover  # _open always sets it
            raise RuntimeError("the laptop database is not open")
        return self._scout

    # ---- daily summaries and the Board's briefings (ADR-039, F17 slice 6)

    async def summary_control(self) -> SummaryControl:
        """The daily summaries and briefings (opens the database on first use)."""
        await self._open()
        if self._summaries is None:  # pragma: no cover  # _open always sets it
            raise RuntimeError("the laptop database is not open")
        return self._summaries

    # ---- the Claude key (ADR-033, F10 slice 2c): sealed after the same passkey

    async def claude_key_control(self) -> ClaudeKeyControl:
        """Saving and opening the Claude key (opens the database on first use)."""
        await self._open()
        if self._claude_key is None:  # pragma: no cover  # _open always sets it
            raise RuntimeError("the laptop database is not open")
        return self._claude_key

    # ---- adding a project (ADR-039, F17 slice 5a): the dashboard acts as the Chairman (no login yet)

    async def _workspaces_now(self) -> WorkspaceControl:
        await self._open()
        if self._workspaces is None:  # pragma: no cover  # _open always sets it
            raise RuntimeError("the laptop database is not open")
        return self._workspaces

    async def add_workspace(self, name: str, repo: str | None) -> Added:
        """Add a project, reading ``repo`` if given (no step-up: nothing in it runs or spends)."""
        return await (await self._workspaces_now()).add(
            name, github=repo, role=Role.CHAIRMAN, channel=Channel.DASHBOARD
        )

    async def repos_in_use(self) -> dict[str, str]:
        """The repository each active or archived workspace reads, by its address."""
        return await (await self._workspaces_now()).repos()

    async def archived(self) -> list[WorkspaceRow]:
        """The archived workspaces."""
        return await (await self._removal_now()).archived()

    async def archive(self, workspace: WorkspaceRow, credential: dict[str, Any] | None) -> Removal:
        """Archive a workspace."""
        return await (await self._removal_now()).archive(workspace, credential)

    async def restore(self, workspace: WorkspaceRow, credential: dict[str, Any] | None) -> LifecycleVerdict:
        """Bring an archived workspace back."""
        return await (await self._removal_now()).restore(workspace, credential)

    async def erase(self, workspace: WorkspaceRow, credential: dict[str, Any] | None) -> LifecycleVerdict:
        """Delete an archived workspace for good."""
        return await (await self._removal_now()).erase(workspace, credential)

    def recorder(self, workspaces: WorkspaceDirectory) -> Inspect:
        """Board inspections (ADR-039), each written to the audit log before anything is read.

        An unknown or archived workspace (the directory is kept current), or an audit log that
        cannot be written, raises: the look is then not made.
        """

        async def record(workspace: str, tool: str, choice: str) -> None:
            found = workspaces.find(workspace)
            if found is None:  # raised before anything is opened
                raise KeyError(workspace)
            workspace_id = found.workspace_id
            await self._open()
            if self._ledger is None:  # pragma: no cover  # _open always sets it
                raise RuntimeError("the laptop database is not open")
            await self._ledger.record(
                AuditDraft(
                    at=self._clock(),
                    actor="user:chairman",
                    action="board.inspection",
                    outcome=AuditOutcome.INFO,
                    reason_code="board.inspection",
                    correlation_id=uuid4(),
                    project_id=workspace_id,
                    target_ref=f"{tool}:{choice}",
                    details={"workspace": workspace, "tool": tool},
                )
            )

        return record

    async def close(self) -> None:
        """Close the laptop database, if it was opened."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def standing(self) -> Standing:
        """How every item stands now (the chat asks before every question)."""
        return await (await self._open()).standing()

    async def page(self) -> RegistryPage:
        """The Customize page as stored now."""
        return await (await self._open()).page()

    async def switch_off(self, kind: Kind, item_id: str) -> Verdict:
        """Switch one item off."""
        return await (await self._open()).switch_off(kind, item_id)

    async def request_on(self, kind: Kind, item_id: str) -> Verdict:
        """Ask for one item to be switched on."""
        return await (await self._open()).request_on(kind, item_id)

    async def withdraw(self, request_id: UUID) -> Verdict:
        """Withdraw a waiting request."""
        return await (await self._open()).withdraw(request_id)

    async def refuse(self, request_id: UUID) -> Verdict:
        """Refuse a waiting request."""
        return await (await self._open()).refuse(request_id)

    async def approve(self, request_id: UUID, credential: dict[str, Any]) -> Verdict:
        """Approve with a Windows Hello signature for this one request."""
        return await (await self._open()).approve(request_id, credential)

    async def approve_options(self, request_id: UUID) -> dict[str, Any]:
        """Options for Windows Hello to sign for this one request."""
        return await (await self._open()).approve_options(request_id)

    async def passkey(self) -> PasskeyStatus:
        """Whether the passkey exists and may be set up now."""
        return await (await self._open()).passkey()

    async def enrol_options(self, code: str) -> dict[str, Any]:
        """Options to make the passkey (needs the start-up code)."""
        return await (await self._open()).enrol_options(code)

    async def enrol(self, code: str, credential: dict[str, Any]) -> None:
        """Keep the new passkey's public half."""
        await (await self._open()).enrol(code, credential)


class LaptopClaudeKeys:
    """The Claude key routes' view of the laptop database (its own object: names clash with the switches')."""

    def __init__(self, switches: LaptopSwitches) -> None:
        self._switches = switches

    async def status(self) -> ClaudeKeyStatus:
        """Whether the vault is ready and a key is saved."""
        return await (await self._switches.claude_key_control()).status()

    async def options(self, key: SecretStr) -> dict[str, Any]:
        """Options for Windows Hello to sign saving exactly ``key``."""
        return await (await self._switches.claude_key_control()).options(key)

    async def save(self, key: SecretStr, credential: dict[str, Any] | None) -> ClaudeKeyVerdict:
        """Seal the key once this exact save is signed."""
        return await (await self._switches.claude_key_control()).save(key, credential)

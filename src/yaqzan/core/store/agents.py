"""Agents per workspace, the frozen role versions they run, and each workspace's switch and cap (ADR-039).

Role versions are the tenant's: written once under their fingerprint, never
changed. Agents and workspace settings belong to the one workspace of the
handle. The database itself refuses an agent created switched on, and any
riskier change (on, a higher cap, a newer role version, another model)
without a fresh passkey step-up time; switching off and lowering a cap are
always allowed.

Spend is read from the run records: what this workspace (or one agent) has
spent since a given time, in minor units of USD.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

import asyncpg

from yaqzan.core.store.db import Scoped, require_aware

Permission = Literal["read", "draft", "act"]

_ADD_VERSION = """
INSERT INTO yaqzan.role_versions (tenant_id, role_id, version, files, permission, stored_at)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (tenant_id, role_id, version) DO NOTHING
"""
_GET_VERSION = """
SELECT role_id, version, files, permission, stored_at FROM yaqzan.role_versions
WHERE tenant_id = $1 AND role_id = $2 AND version = $3
"""
_ADD_AGENT = """
INSERT INTO yaqzan.agents (tenant_id, project_id, agent_id, role_id, version, enabled, model, cap_minor,
    created_by, created_at, changed_by, changed_at, step_up_at)
VALUES ($1, $2, $3, $4, $5, false, $6, NULL, $7, $8, $7, $8, NULL)
ON CONFLICT (tenant_id, project_id, role_id) DO NOTHING
RETURNING agent_id
"""
_AGENTS = """
SELECT agent_id, role_id, version, enabled, model, cap_minor, created_by, created_at, changed_by, changed_at,
    step_up_at
FROM yaqzan.agents WHERE tenant_id = $1 AND project_id = $2 ORDER BY created_at
"""
_AGENT = """
SELECT agent_id, role_id, version, enabled, model, cap_minor, created_by, created_at, changed_by, changed_at,
    step_up_at
FROM yaqzan.agents WHERE tenant_id = $1 AND project_id = $2 AND agent_id = $3
"""
_SET_AGENT = """
UPDATE yaqzan.agents SET enabled = $4, cap_minor = $5, changed_by = $6, changed_at = $7, step_up_at = $8
WHERE tenant_id = $1 AND project_id = $2 AND agent_id = $3
RETURNING agent_id
"""
_SETTINGS = """
SELECT enabled, cap_minor, changed_by, changed_at, step_up_at FROM yaqzan.workspace_settings
WHERE tenant_id = $1 AND project_id = $2
"""
_PUT_SETTINGS = """
INSERT INTO yaqzan.workspace_settings (tenant_id, project_id, enabled, cap_minor, changed_by, changed_at,
    step_up_at)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (tenant_id, project_id) DO UPDATE SET enabled = EXCLUDED.enabled, cap_minor = EXCLUDED.cap_minor,
    changed_by = EXCLUDED.changed_by, changed_at = EXCLUDED.changed_at, step_up_at = EXCLUDED.step_up_at
"""
_SPENT = """
SELECT coalesce(sum(cost_minor_units), 0)::bigint FROM yaqzan.runs
WHERE tenant_id = $1 AND project_id = $2 AND started_at >= $3
"""
_SPENT_BY = """
SELECT coalesce(sum(cost_minor_units), 0)::bigint FROM yaqzan.runs
WHERE tenant_id = $1 AND project_id = $2 AND agent = $3 AND started_at >= $4
"""


@dataclass(frozen=True)
class RoleVersion:
    """One frozen copy of a role pack: its files' text, by file name, and its highest permission."""

    role_id: str
    version: str
    files: dict[str, str]
    permission: Permission
    stored_at: datetime


@dataclass(frozen=True)
class NewAgent:
    """What is needed to give a workspace an agent: it always starts switched off, with no cap."""

    agent_id: UUID
    role_id: str
    version: str
    model: str
    by: str
    at: datetime


@dataclass(frozen=True)
class AgentRow:
    """One agent of a workspace. ``cap_minor`` ``None`` means no cap set (so it may not spend)."""

    agent_id: UUID
    role_id: str
    version: str
    enabled: bool
    model: str
    cap_minor: int | None
    created_by: str
    created_at: datetime
    changed_by: str
    changed_at: datetime
    step_up_at: datetime | None


@dataclass(frozen=True)
class WorkspaceSettings:
    """A workspace's switch and monthly cap. ``cap_minor`` ``None`` means none set (so nothing may spend)."""

    enabled: bool
    cap_minor: int | None
    changed_by: str
    changed_at: datetime
    step_up_at: datetime | None


@dataclass(frozen=True)
class AgentChange:
    """A new switch and cap for one agent, who set them, when, and the passkey step-up behind it if any."""

    enabled: bool
    cap_minor: int | None
    by: str
    at: datetime
    step_up_at: datetime | None


def _agent(record: asyncpg.Record | None) -> AgentRow | None:
    return None if record is None else AgentRow(**dict(record))


class RoleVersionRows:
    """Repository for the tenant's frozen role versions."""

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope
        self._tenant = scope.tenant_id

    async def put(self, version: RoleVersion) -> None:
        """Store a role version once; storing the same fingerprint again changes nothing."""
        require_aware(version.stored_at)
        async with self._scope.write("a frozen role version") as conn:
            await conn.execute(
                _ADD_VERSION,
                self._tenant,
                version.role_id,
                version.version,
                version.files,
                version.permission,
                version.stored_at,
            )

    async def get(self, role_id: str, version: str) -> RoleVersion | None:
        """One stored role version, or None."""
        async with self._scope.connection() as conn:
            record = await conn.fetchrow(_GET_VERSION, self._tenant, role_id, version)
        return None if record is None else RoleVersion(**dict(record))


class AgentRows:
    """Repository for the agents and the switch and cap of the one workspace of its handle."""

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope
        self._key = (scope.tenant_id, scope.require_workspace())

    async def add(self, agent: NewAgent) -> bool:
        """Give the workspace an agent, switched off. False if it already has one for that role."""
        require_aware(agent.at)
        async with self._scope.write("a new agent") as conn:
            added = await conn.fetchval(
                _ADD_AGENT,
                *self._key,
                agent.agent_id,
                agent.role_id,
                agent.version,
                agent.model,
                agent.by,
                agent.at,
            )
            return added is not None

    async def agents(self) -> list[AgentRow]:
        """Every agent of the workspace, oldest first."""
        async with self._scope.connection() as conn:
            records = await conn.fetch(_AGENTS, *self._key)
        return [AgentRow(**dict(r)) for r in records]

    async def get(self, agent_id: UUID) -> AgentRow | None:
        """One agent of this workspace, or None (an agent of another workspace is not found)."""
        async with self._scope.connection() as conn:
            return _agent(await conn.fetchrow(_AGENT, *self._key, agent_id))

    async def change(self, agent_id: UUID, change: AgentChange) -> bool:
        """Set an agent's switch and cap. False if there is no such agent in this workspace."""
        require_aware(change.at, change.step_up_at)
        async with self._scope.write("an agent's switch or cap") as conn:
            changed = await conn.fetchval(
                _SET_AGENT,
                *self._key,
                agent_id,
                change.enabled,
                change.cap_minor,
                change.by,
                change.at,
                change.step_up_at,
            )
            return changed is not None

    async def settings(self) -> WorkspaceSettings | None:
        """The workspace's switch and cap, or None if never set (off, no cap)."""
        async with self._scope.connection() as conn:
            record = await conn.fetchrow(_SETTINGS, *self._key)
        return None if record is None else WorkspaceSettings(**dict(record))

    async def put_settings(self, settings: WorkspaceSettings) -> None:
        """Store the workspace's switch and cap."""
        require_aware(settings.changed_at, settings.step_up_at)
        async with self._scope.write("a workspace's switch or cap") as conn:
            await conn.execute(
                _PUT_SETTINGS,
                *self._key,
                settings.enabled,
                settings.cap_minor,
                settings.changed_by,
                settings.changed_at,
                settings.step_up_at,
            )

    async def spent(self, since: datetime, agent_id: UUID | None = None) -> int:
        """Minor units spent since ``since``: the whole workspace, or one agent's runs."""
        require_aware(since)
        async with self._scope.connection() as conn:
            if agent_id is None:
                return int(await conn.fetchval(_SPENT, *self._key, since))
            return int(await conn.fetchval(_SPENT_BY, *self._key, str(agent_id), since))

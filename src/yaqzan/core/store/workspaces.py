"""Tenants and their workspaces: one workspace per project (ADR-039, F17).

A workspace must exist before any project-level row can be saved in it (the
database refuses the row otherwise), so a tenant-wide job can go through the
list of workspaces and know it missed none. Both tables sit behind the tenant
wall: a tenant sees only its own workspaces.

A workspace can be archived (everything in it disconnected, its history kept)
and then brought back or deleted for good (migration 0008). Only active
workspaces are listed by :meth:`WorkspaceRows.workspaces`; a deleted one stays
only as a marker, with no data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from yaqzan.core.store.db import Scoped, require_aware
from yaqzan.core.store.errors import StoreError, StoreErrorCode

_ADD_TENANT = """
INSERT INTO yaqzan.tenants (tenant_id, name) VALUES ($1, $2)
ON CONFLICT (tenant_id) DO NOTHING
"""
_ADD_WORKSPACE = """
INSERT INTO yaqzan.workspaces (tenant_id, workspace_id, slug, name) VALUES ($1, $2, $3, $4)
ON CONFLICT (tenant_id, workspace_id) DO NOTHING
RETURNING workspace_id
"""
_ADD_SOURCE = """
INSERT INTO yaqzan.workspace_sources (tenant_id, project_id, kind, ref, added_by, added_at)
VALUES ($1, $2, 'github', $3, $4, $5)
"""
_SOURCE = """
SELECT ref FROM yaqzan.workspace_sources WHERE tenant_id = $1 AND project_id = $2 AND kind = 'github'
"""
_SLUGS = "SELECT slug FROM yaqzan.workspaces WHERE tenant_id = $1"
_RENAME = "UPDATE yaqzan.workspaces SET name = $3 WHERE tenant_id = $1 AND workspace_id = $2"
_LIST = """
SELECT workspace_id, slug, name FROM yaqzan.workspaces WHERE tenant_id = $1 AND state = $2
ORDER BY created_at, slug
"""
_AGENTS_OFF = """
UPDATE yaqzan.agents SET enabled = false, cap_minor = NULL, changed_by = $3, changed_at = $4
WHERE tenant_id = $1 AND project_id = $2 AND (enabled OR cap_minor IS NOT NULL)
"""
_WORKSPACE_OFF = """
INSERT INTO yaqzan.workspace_settings (tenant_id, project_id, enabled, cap_minor, changed_by, changed_at)
VALUES ($1, $2, false, NULL, $3, $4)
ON CONFLICT (tenant_id, project_id) DO UPDATE SET enabled = false, cap_minor = NULL,
    changed_by = EXCLUDED.changed_by, changed_at = EXCLUDED.changed_at
"""
_FORGET_KEYS = "DELETE FROM yaqzan.sealed_secrets WHERE tenant_id = $1 AND project_id = $2"
_MOVE = """
UPDATE yaqzan.workspaces SET state = $4, state_changed_by = $5, state_changed_at = $6, step_up_at = $7
WHERE tenant_id = $1 AND workspace_id = $2 AND state = $3
"""
_ERASE = "SELECT yaqzan.erase_workspace($1, $2, $3, $4)"
_STATE = "SELECT state FROM yaqzan.workspaces WHERE tenant_id = $1 AND workspace_id = $2 FOR UPDATE"


@dataclass(frozen=True)
class WorkspaceRow:
    """One workspace of a tenant. ``slug`` is for addresses on screen; storage uses ``workspace_id``."""

    workspace_id: UUID
    slug: str
    name: str


@dataclass(frozen=True)
class StateChange:
    """Who moved a workspace, when, and the passkey step-up that allowed it."""

    by: str
    at: datetime
    step_up_at: datetime


@dataclass(frozen=True)
class Archived:
    """What archiving switched off and deleted."""

    agents_stopped: int
    keys_deleted: int


class WorkspaceRows:
    """Repository for the tenant of its handle and that tenant's workspaces."""

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope
        self._tenant = scope.tenant_id

    async def add_tenant(self, name: str) -> None:
        """Create the tenant if it does not exist yet."""
        async with self._scope.write("a tenant") as conn:
            await conn.execute(_ADD_TENANT, self._tenant, name)

    async def add(self, workspace: WorkspaceRow) -> bool:
        """Create a workspace. Returns False if it already existed (its name is then left alone)."""
        async with self._scope.write("a workspace") as conn:
            added = await conn.fetchval(
                _ADD_WORKSPACE, self._tenant, workspace.workspace_id, workspace.slug, workspace.name
            )
            return added is not None

    async def create(self, workspace: WorkspaceRow, *, github: str | None, by: str, at: datetime) -> None:
        """Add a new workspace and, if given, the GitHub repository it reads: both, or neither.

        The handle must be for the new workspace itself (its id chosen by the caller), so the
        repository row lands behind that workspace's wall. A slug or repository already taken
        fails as :class:`StoreError` ``store.conflict``.
        """
        if self._scope.workspace_id != workspace.workspace_id:
            raise ValueError("create a workspace through a handle for that workspace")
        require_aware(at)
        async with self._scope.write("a new workspace") as conn:
            added = await conn.fetchval(
                _ADD_WORKSPACE, self._tenant, workspace.workspace_id, workspace.slug, workspace.name
            )
            if added is None:
                raise StoreError(StoreErrorCode.CONFLICT, "this workspace already exists")
            if github is not None:
                await conn.execute(_ADD_SOURCE, self._tenant, workspace.workspace_id, github, by, at)

    async def slugs(self) -> set[str]:
        """Every address the tenant has used, archived and deleted ones too (they stay reserved)."""
        async with self._scope.connection() as conn:
            return {row["slug"] for row in await conn.fetch(_SLUGS, self._tenant)}

    async def rename(self, workspace_id: UUID, name: str) -> None:
        """Give a workspace a new display name."""
        async with self._scope.write("a workspace's new name") as conn:
            await conn.execute(_RENAME, self._tenant, workspace_id, name)

    async def workspaces(self) -> list[WorkspaceRow]:
        """Every active workspace of the tenant, in the order they were added."""
        return await self._listed("active")

    async def archived(self) -> list[WorkspaceRow]:
        """The tenant's archived workspaces (they can be brought back or deleted for good)."""
        return await self._listed("archived")

    async def _listed(self, state: str) -> list[WorkspaceRow]:
        async with self._scope.connection() as conn:
            rows = await conn.fetch(_LIST, self._tenant, state)
        return [WorkspaceRow(**dict(row)) for row in rows]


class WorkspaceSources:
    """The repository the one workspace of its handle reads."""

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope
        self._key = (scope.tenant_id, scope.require_workspace())

    async def github(self) -> str | None:
        """The GitHub repository (``owner/name``), or ``None`` if none is connected."""
        async with self._scope.connection() as conn:
            found: str | None = await conn.fetchval(_SOURCE, *self._key)
        return found

    async def connect_github(self, repo: str, *, by: str, at: datetime) -> None:
        """Connect a GitHub repository to a workspace that has none (the laptop set-up's projects)."""
        require_aware(at)
        async with self._scope.write("a workspace's repository") as conn:
            await conn.execute(_ADD_SOURCE, *self._key, repo, by, at)


class WorkspaceLifecycle:
    """Archive, bring back or delete for good the one workspace of its handle.

    Each is one transaction, so a workspace is never left half disconnected.
    The database checks the step-up and the order (only an archived workspace
    can be deleted) whatever the code above does.
    """

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope
        self._key = (scope.tenant_id, scope.require_workspace())

    async def archive(self, change: StateChange) -> Archived | None:
        """Switch everything off, delete its saved keys, mark it archived. ``None``: it was not active."""
        async with self._scope.write("an archived workspace") as conn:
            # Held until the end, so nothing can change the workspace's state half way.
            if await conn.fetchval(_STATE, *self._key) != "active":
                return None
            stopped = await conn.execute(_AGENTS_OFF, *self._key, change.by, change.at)
            await conn.execute(_WORKSPACE_OFF, *self._key, change.by, change.at)
            forgotten = await conn.execute(_FORGET_KEYS, *self._key)
            await conn.execute(_MOVE, *self._key, "active", "archived", *_fields(change))
        return Archived(agents_stopped=_count(stopped), keys_deleted=_count(forgotten))

    async def restore(self, change: StateChange) -> bool:
        """Bring an archived workspace back (its agents stay off). False if it was not archived."""
        async with self._scope.write("a workspace brought back") as conn:
            moved = await conn.execute(_MOVE, *self._key, "archived", "active", *_fields(change))
        return _count(moved) == 1

    async def erase(self, change: StateChange) -> None:
        """Delete an archived workspace's data for good; only its marker stays."""
        async with self._scope.write("a workspace deleted for good") as conn:
            await conn.execute(_ERASE, self._key[1], change.by, change.step_up_at, change.at)


def _fields(change: StateChange) -> tuple[str, datetime, datetime]:
    return change.by, change.at, change.step_up_at


def _count(status: str) -> int:
    """Rows touched, from a command status such as ``UPDATE 3``."""
    return int(status.rsplit(" ", 1)[-1])

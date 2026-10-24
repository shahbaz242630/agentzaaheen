"""Tenants and their workspaces: one workspace per project (ADR-039, F17).

A workspace must exist before any project-level row can be saved in it (the
database refuses the row otherwise), so a tenant-wide job can go through the
list of workspaces and know it missed none. Both tables sit behind the tenant
wall: a tenant sees only its own workspaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from yaqzan.core.store.db import Database

_ADD_TENANT = """
INSERT INTO yaqzan.tenants (tenant_id, name) VALUES ($1, $2)
ON CONFLICT (tenant_id) DO NOTHING
"""
_ADD_WORKSPACE = """
INSERT INTO yaqzan.workspaces (tenant_id, workspace_id, slug, name) VALUES ($1, $2, $3, $4)
ON CONFLICT (tenant_id, workspace_id) DO NOTHING
RETURNING workspace_id
"""
_RENAME = "UPDATE yaqzan.workspaces SET name = $3 WHERE tenant_id = $1 AND workspace_id = $2"
_LIST = """
SELECT workspace_id, slug, name FROM yaqzan.workspaces WHERE tenant_id = $1 ORDER BY slug
"""


@dataclass(frozen=True)
class WorkspaceRow:
    """One workspace of a tenant. ``slug`` is for addresses on screen; storage uses ``workspace_id``."""

    workspace_id: UUID
    slug: str
    name: str


class WorkspaceRows:
    """Repository for tenants and workspaces."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add_tenant(self, tenant_id: UUID, name: str) -> None:
        """Create the tenant if it does not exist yet."""
        async with self._db.scoped(tenant_id).write("a tenant") as conn:
            await conn.execute(_ADD_TENANT, tenant_id, name)

    async def add(self, tenant_id: UUID, workspace: WorkspaceRow) -> bool:
        """Create a workspace. Returns False if it already existed (its name is then left alone)."""
        async with self._db.scoped(tenant_id).write("a workspace") as conn:
            added = await conn.fetchval(
                _ADD_WORKSPACE, tenant_id, workspace.workspace_id, workspace.slug, workspace.name
            )
            return added is not None

    async def rename(self, tenant_id: UUID, workspace_id: UUID, name: str) -> None:
        """Give a workspace a new display name."""
        async with self._db.scoped(tenant_id).write("a workspace's new name") as conn:
            await conn.execute(_RENAME, tenant_id, workspace_id, name)

    async def workspaces(self, tenant_id: UUID) -> list[WorkspaceRow]:
        """Every workspace of the tenant, by slug."""
        async with self._db.scoped(tenant_id).connection() as conn:
            rows = await conn.fetch(_LIST, tenant_id)
        return [WorkspaceRow(**dict(row)) for row in rows]

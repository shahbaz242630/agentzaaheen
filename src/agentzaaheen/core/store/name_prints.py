"""Our customers' names as keyed fingerprints only (migration 0013, F10 slice 2b, ADR-023).

A workspace's prints are added and never changed or removed by the app: a
customer who has left is still protected. The tenant-wide read gathers every
workspace's prints (active and archived), one workspace at a time behind each
wall, for the customer-data check. No name is ever stored or returned here.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from agentzaaheen.core.store.db import Scoped, require_aware
from agentzaaheen.core.store.workspaces import WorkspaceRows

MAX_PRINTS = 200_000  # per add: far above any app's customer list
PRINT_KINDS = frozenset({"full", "first", "single"})

_ADD = """
INSERT INTO agentzaaheen.customer_name_prints (tenant_id, project_id, key_id, kind, print, added_at)
SELECT $1, $2, $3, kind, print, $6 FROM unnest($4::text[], $5::bytea[]) AS given (kind, print)
ON CONFLICT DO NOTHING
"""
_READ = """
SELECT kind, print FROM agentzaaheen.customer_name_prints
WHERE tenant_id = $1 AND project_id = $2 AND key_id = $3
"""


class NamePrints:
    """The name prints of the one workspace of its handle."""

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope
        self._key = (scope.tenant_id, scope.require_workspace())

    async def add(self, prints: Iterable[tuple[str, bytes]], *, key_id: str, at: datetime) -> int:
        """Add ``(kind, print)`` pairs made with ``key_id``; returns how many were new."""
        require_aware(at)
        given = sorted(set(prints))
        if len(given) > MAX_PRINTS:
            raise ValueError(f"at most {MAX_PRINTS} prints at a time")
        if any(kind not in PRINT_KINDS for kind, _ in given):
            raise ValueError("a print's kind is full, first or single")
        kinds, values = [kind for kind, _ in given], [value for _, value in given]
        async with self._scope.write("customer name prints") as conn:
            status = await conn.execute(_ADD, *self._key, key_id, kinds, values, at)
        return int(status.rsplit(" ", 1)[-1])


class AllNamePrints:
    """The tenant-wide read of name prints, one workspace at a time behind each wall."""

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope

    async def made_with(self, key_id: str) -> set[tuple[str, bytes]]:
        """Every ``(kind, print)`` made with ``key_id``, across active and archived workspaces."""
        rows = WorkspaceRows(self._scope)
        out: set[tuple[str, bytes]] = set()
        for workspace in [*await rows.workspaces(), *await rows.archived()]:
            where = self._scope.within(workspace.workspace_id)
            async with where.connection() as conn:
                records = await conn.fetch(_READ, where.tenant_id, where.workspace_id, key_id)
            out.update((record["kind"], bytes(record["print"])) for record in records)
        return out

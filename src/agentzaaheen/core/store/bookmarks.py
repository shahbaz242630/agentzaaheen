"""Where each agent got to: its bookmarks (migration 0017, F11 slice 4).

An agent reads its bookmarks when a run starts (the runner hands them over on
the run ticket) and says where it got to when it ends. The runner moves them
only through :meth:`~agentzaaheen.core.store.records.Records.end_run`, in the
same transaction as the run's end and its findings, after the findings: a
bookmark never moves past something that was not saved.
"""

from __future__ import annotations

from uuid import UUID

from agentzaaheen.core.store.db import Scoped

MAX_BOOKMARKS = 20  # per agent; an agent that needs more is keeping a list, not a bookmark

_OF = """
SELECT name, value FROM agentzaaheen.bookmarks WHERE tenant_id = $1 AND project_id = $2 AND agent_id = $3
ORDER BY name LIMIT 20
"""
MOVE_BOOKMARK = """
INSERT INTO agentzaaheen.bookmarks (tenant_id, project_id, agent_id, name, value, run_id, moved_at)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (tenant_id, project_id, agent_id, name)
DO UPDATE SET value = excluded.value, run_id = excluded.run_id, moved_at = excluded.moved_at
"""


class Bookmarks:
    """The bookmarks of the handle's one workspace."""

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope
        self._key = (scope.tenant_id, scope.require_workspace())

    async def of(self, agent_id: UUID) -> dict[str, str]:
        """Every bookmark of ``agent_id`` (none yet: empty)."""
        async with self._scope.connection() as conn:
            rows = await conn.fetch(_OF, *self._key, agent_id)
        return {row["name"]: row["value"] for row in rows}

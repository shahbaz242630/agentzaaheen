"""Kill-switch rows: stop everything, freeze one project, or pause one agent (SECURITY §6.8)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from yaqzan.core.store.db import Database, require_aware

_SET = """
INSERT INTO yaqzan.kill_switches (tenant_id, scope, ref, engaged, changed_by, changed_at)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (tenant_id, scope, ref) DO UPDATE SET
    engaged = EXCLUDED.engaged, changed_by = EXCLUDED.changed_by, changed_at = EXCLUDED.changed_at
"""
_ENGAGED = "SELECT scope, ref FROM yaqzan.kill_switches WHERE tenant_id = $1 AND engaged ORDER BY scope, ref"


class SwitchScope(StrEnum):
    """What a switch covers."""

    ALL = "all"
    PROJECT = "project"
    AGENT = "agent"


@dataclass(frozen=True)
class EngagedSwitch:
    """One switch that is currently on. ``ref`` is empty for ``ALL``."""

    scope: SwitchScope
    ref: str


class KillSwitchRows:
    """Repository for kill-switch state."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def set(  # noqa: PLR0913  # each argument is a separate column
        self,
        tenant_id: UUID,
        scope: SwitchScope,
        ref: str,
        *,
        engaged: bool,
        changed_by: str,
        changed_at: datetime,
    ) -> None:
        """Turn one switch on or off."""
        require_aware(changed_at)
        async with self._db.write("a kill switch") as conn:
            await conn.execute(_SET, tenant_id, scope.value, ref, engaged, changed_by, changed_at)

    async def engaged(self, tenant_id: UUID) -> list[EngagedSwitch]:
        """Every switch that is on for this tenant."""
        async with self._db.connection() as conn:
            records = await conn.fetch(_ENGAGED, tenant_id)
        return [EngagedSwitch(scope=SwitchScope(r["scope"]), ref=r["ref"]) for r in records]

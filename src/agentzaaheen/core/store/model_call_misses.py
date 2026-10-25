"""Each Claude run that did not run, and why (migration 0014, F10 slice 3a, ADR-033).

One row per cloud ask that ended without an answer, added once and never
changed. Codes only: no prompt, no answer. The morning briefing reads the
last day's rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from agentzaaheen.core.store.db import Scoped, require_aware

_ADD = """
INSERT INTO agentzaaheen.model_call_misses (tenant_id, miss_id, at, role, use, model, outcome, detail, run_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
"""
_SINCE = """
SELECT at, role, use, model, outcome, detail, run_id FROM agentzaaheen.model_call_misses
WHERE tenant_id = $1 AND at >= $2 AND at < $3
ORDER BY at, miss_id
LIMIT $4
"""


@dataclass(frozen=True)
class MissRow:
    """One run that did not run: when, who, what for, which model, and why (codes only)."""

    at: datetime
    role: str
    use: str
    model: str
    outcome: str
    detail: str | None
    run_id: str


class ModelCallMisses:
    """The tenant's Claude runs that did not run (a tenant-level handle, like ``ModelCalls``)."""

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope
        self._tenant = scope.tenant_id

    async def add(self, miss: MissRow) -> UUID:
        """Save one miss; returns the new row's id."""
        require_aware(miss.at)
        miss_id = uuid4()
        async with self._scope.write("a Claude run that did not run") as conn:
            await conn.execute(
                _ADD,
                self._tenant,
                miss_id,
                miss.at,
                miss.role,
                miss.use,
                miss.model,
                miss.outcome,
                miss.detail,
                miss.run_id,
            )
        return miss_id

    async def between(self, start: datetime, end: datetime, *, limit: int = 200) -> list[MissRow]:
        """The misses from ``start`` (included) to ``end`` (not included), oldest first, at most ``limit``."""
        require_aware(start, end)
        async with self._scope.connection() as conn:
            records = await conn.fetch(_SINCE, self._tenant, start, end, limit)
        return [MissRow(**dict(record)) for record in records]

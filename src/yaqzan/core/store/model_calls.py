"""What each Claude call cost (migration 0012, F10 slice 2b, ADR-033).

One row per call that was sent, added once and never changed: the spend caps
add up a month's rows, so no restart can reset them. No prompt, no answer:
only when, what for, which model and the cost in millionths of a USD.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from yaqzan.core.store.db import Scoped, require_aware

_ADD = """
INSERT INTO yaqzan.model_calls (tenant_id, call_id, at, use, model, cost_micro)
VALUES ($1, $2, $3, $4, $5, $6)
"""
_SPENT = """
SELECT coalesce(sum(cost_micro), 0)::bigint FROM yaqzan.model_calls
WHERE tenant_id = $1 AND use = $2 AND at >= $3 AND at < $4
"""


class ModelCalls:
    """The tenant's Claude calls (a tenant-level handle: the Board's spend, not one workspace's)."""

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope
        self._tenant = scope.tenant_id

    async def add(self, use: str, model: str, cost_micro: int, *, at: datetime) -> UUID:
        """Save one call's cost; returns the new row's id."""
        require_aware(at)
        if cost_micro < 0:
            raise ValueError("a cost cannot be negative")
        call_id = uuid4()
        async with self._scope.write("a model call's cost") as conn:
            await conn.execute(_ADD, self._tenant, call_id, at, use, model, cost_micro)
        return call_id

    async def spent(self, use: str, *, start: datetime, end: datetime) -> int:
        """What ``use`` cost from ``start`` (included) to ``end`` (not included)."""
        require_aware(start, end)
        async with self._scope.connection() as conn:
            total = await conn.fetchval(_SPENT, self._tenant, use, start, end)
        return int(total)

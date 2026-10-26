"""What each Claude call cost (migration 0012, F10 slice 2b, ADR-033), and its token counts (0015, slice 3b).

One row per call that was sent, added once and never changed: the spend caps
add up a month's rows, so no restart can reset them. No prompt, no answer:
only when, what for, which model, the cost in millionths of a USD and, since
migration 0015, the call's token counts and the visible answer's length; since
0016, the agent run it was for (so a run's cost can be added up, F11).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from agentzaaheen.core.store.db import Scoped, require_aware

_ADD = """
INSERT INTO agentzaaheen.model_calls (tenant_id, call_id, at, use, model, cost_micro,
    tokens_in, tokens_out, cache_read, cache_write, answer_chars, run_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
"""
_SPENT = """
SELECT coalesce(sum(cost_micro), 0)::bigint FROM agentzaaheen.model_calls
WHERE tenant_id = $1 AND use = $2 AND at >= $3 AND at < $4
"""
_FIRST = """
SELECT at, use, model, cost_micro, tokens_in, tokens_out, cache_read, cache_write, answer_chars
FROM agentzaaheen.model_calls
WHERE tenant_id = $1 AND model = $2 AND tokens_out IS NOT NULL
ORDER BY at, call_id
LIMIT $3
"""


@dataclass(frozen=True)
class CallTokens:
    """One call's token counts and the visible answer's length in characters. Counts only."""

    tokens_in: int
    tokens_out: int
    cache_read: int
    cache_write: int
    answer_chars: int


@dataclass(frozen=True)
class CallRow:
    """One saved call with its counts (calls saved before migration 0015 have none and are not read here)."""

    at: datetime
    use: str
    model: str
    cost_micro: int
    tokens: CallTokens


class ModelCalls:
    """The tenant's Claude calls (a tenant-level handle: the Board's spend, not one workspace's)."""

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope
        self._tenant = scope.tenant_id

    async def add(  # noqa: PLR0913  # each is a separate column
        self,
        use: str,
        model: str,
        cost_micro: int,
        *,
        at: datetime,
        tokens: CallTokens | None = None,
        run_id: str | None = None,
    ) -> UUID:
        """Save one call's cost (and its counts and run, when known); returns the new row's id."""
        require_aware(at)
        if cost_micro < 0:
            raise ValueError("a cost cannot be negative")
        counts = (
            (None,) * 5
            if tokens is None
            else (
                tokens.tokens_in,
                tokens.tokens_out,
                tokens.cache_read,
                tokens.cache_write,
                tokens.answer_chars,
            )
        )
        call_id = uuid4()
        async with self._scope.write("a model call's cost") as conn:
            await conn.execute(_ADD, self._tenant, call_id, at, use, model, cost_micro, *counts, run_id)
        return call_id

    async def spent(self, use: str, *, start: datetime, end: datetime) -> int:
        """What ``use`` cost from ``start`` (included) to ``end`` (not included)."""
        require_aware(start, end)
        async with self._scope.connection() as conn:
            total = await conn.fetchval(_SPENT, self._tenant, use, start, end)
        return int(total)

    async def first_with_counts(self, model: str, limit: int) -> list[CallRow]:
        """The first ``limit`` calls to ``model`` that kept their counts, oldest first."""
        async with self._scope.connection() as conn:
            records = await conn.fetch(_FIRST, self._tenant, model, limit)
        return [
            CallRow(
                at=r["at"],
                use=r["use"],
                model=r["model"],
                cost_micro=r["cost_micro"],
                tokens=CallTokens(
                    r["tokens_in"], r["tokens_out"], r["cache_read"], r["cache_write"], r["answer_chars"]
                ),
            )
            for r in records
        ]

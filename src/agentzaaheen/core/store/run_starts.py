"""Each agent run, written down before it starts; closing those that never ended (migration 0016, F11).

The runner writes a :class:`RunStart` before the agent does anything, and the
run's ``runs`` row when it ends. :meth:`RunStarts.close_interrupted` closes a
start that has no end after its deadline as ``could_not_run`` /
``run.interrupted``, with the cost of any Claude calls it made, so a run that
died halfway is still named and still counts against its caps.
:meth:`RunStarts.missed` lists the runs that did not run in a window, for the
workspace's daily summary. Codes and our own role ids only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from agentzaaheen.core.store.db import Scoped, require_aware, written

RUN_INTERRUPTED = "run.interrupted"
MICRO_PER_MINOR = 10_000  # millionths of a USD per cent

_OPEN = """
INSERT INTO agentzaaheen.run_starts (tenant_id, project_id, run_id, agent_id, role_id, version, started_at,
    deadline)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (tenant_id, run_id) DO NOTHING
RETURNING run_id
"""
# Cost rounds up to the next cent, so a run is never counted as cheaper than it was. ``sum`` of bigints
# is numeric: ``div`` divides whole numbers (``/`` would round 150.9999 to 151).
_CALLS = """
SELECT coalesce(div(sum(cost_micro) + 9999, 10000), 0)::bigint AS cost_minor,
    coalesce(sum(tokens_in), 0)::bigint AS tokens_in, coalesce(sum(tokens_out), 0)::bigint AS tokens_out
FROM agentzaaheen.model_calls WHERE tenant_id = $1 AND run_id = $2
"""
_CLOSE_INTERRUPTED = """
INSERT INTO agentzaaheen.runs (id, tenant_id, project_id, agent, agent_version, trigger_event_ids,
    prompt_hash, model, data_class_max, trust, steps, tokens_in, tokens_out, cost_minor_units, started_at,
    duration_ms, outcome, error_code)
SELECT s.run_id, s.tenant_id, s.project_id, s.agent_id::text, s.version, '{}', s.version, NULL, 'public',
    'trusted', 0,
    coalesce((SELECT sum(m.tokens_in) FROM agentzaaheen.model_calls m
        WHERE m.tenant_id = s.tenant_id AND m.run_id = s.run_id::text), 0),
    coalesce((SELECT sum(m.tokens_out) FROM agentzaaheen.model_calls m
        WHERE m.tenant_id = s.tenant_id AND m.run_id = s.run_id::text), 0),
    coalesce((SELECT div(sum(m.cost_micro) + 9999, 10000) FROM agentzaaheen.model_calls m
        WHERE m.tenant_id = s.tenant_id AND m.run_id = s.run_id::text), 0),
    s.started_at, (extract(epoch FROM s.deadline - s.started_at) * 1000)::bigint, 'could_not_run', $4
FROM agentzaaheen.run_starts s
WHERE s.tenant_id = $1 AND s.project_id = $2 AND s.deadline < $3
    AND NOT EXISTS (SELECT 1 FROM agentzaaheen.runs r WHERE r.id = s.run_id)
ON CONFLICT (id) DO NOTHING
RETURNING id
"""
_MISSED = """
SELECT r.id, coalesce(a.role_id, 'unknown') AS role_id, r.outcome, r.error_code, r.started_at
FROM agentzaaheen.runs r
LEFT JOIN agentzaaheen.agents a
    ON a.tenant_id = r.tenant_id AND a.project_id = r.project_id AND a.agent_id::text = r.agent
WHERE r.tenant_id = $1 AND r.project_id = $2 AND r.started_at >= $3 AND r.started_at < $4
    AND r.outcome NOT IN ('ok', 'nothing_found')
ORDER BY r.started_at, r.id
LIMIT $5
"""


@dataclass(frozen=True)
class RunStart:
    """A run about to start: which agent, which frozen role version, and by when it must end."""

    run_id: UUID
    agent_id: UUID
    role_id: str
    version: str
    started_at: datetime
    deadline: datetime


@dataclass(frozen=True)
class RunCalls:
    """What a run's Claude calls added up to: cost in cents (rounded up) and tokens."""

    cost_minor: int
    tokens_in: int
    tokens_out: int


@dataclass(frozen=True)
class MissedRun:
    """A run that did not run: which role, how it ended and why (codes only)."""

    run_id: UUID
    role_id: str
    outcome: str
    error_code: str | None
    started_at: datetime


class RunStarts:
    """The run starts of the handle's one workspace."""

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope
        self._key = (scope.tenant_id, scope.require_workspace())

    async def open(self, start: RunStart) -> bool:
        """Write the start down; ``False`` if a run with this id was already started."""
        require_aware(start.started_at, start.deadline)
        async with self._scope.write("an agent run's start") as conn:
            row = await conn.fetchval(
                _OPEN,
                *self._key,
                start.run_id,
                start.agent_id,
                start.role_id,
                start.version,
                start.started_at,
                start.deadline,
            )
            return written(row)

    async def calls(self, run_id: UUID) -> RunCalls:
        """What the run's Claude calls cost so far (none: all zero)."""
        async with self._scope.connection() as conn:
            row = await conn.fetchrow(_CALLS, self._key[0], str(run_id))
        if row is None:  # pragma: no cover  # an aggregate always gives one row
            return RunCalls(0, 0, 0)
        return RunCalls(int(row["cost_minor"]), int(row["tokens_in"]), int(row["tokens_out"]))

    async def close_interrupted(self, now: datetime) -> list[UUID]:
        """Close every start past its deadline that has no end, as ``run.interrupted``; their ids."""
        require_aware(now)
        async with self._scope.write("runs that never ended, closed as interrupted") as conn:
            rows = await conn.fetch(_CLOSE_INTERRUPTED, *self._key, now, RUN_INTERRUPTED)
            written(rows or None)
        return [r["id"] for r in rows]

    async def missed(self, start: datetime, end: datetime, *, limit: int = 50) -> list[MissedRun]:
        """Runs started from ``start`` (included) to ``end`` (not) that did not run, oldest first."""
        require_aware(start, end)
        async with self._scope.connection() as conn:
            rows = await conn.fetch(_MISSED, *self._key, start, end, limit)
        return [
            MissedRun(r["id"], r["role_id"], r["outcome"], r["error_code"], r["started_at"]) for r in rows
        ]

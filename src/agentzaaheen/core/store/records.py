"""Save and load the SDK records: Event, Finding, Action and RunRecord (CONTRACTS §1-4).

Records go in and come out as the SDK's own pydantic models, so every rule the
model enforces is checked again when a row is read back. Every query works
inside one tenant's workspace (ADR-039): a row from another workspace or
tenant is simply not found.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any
from uuid import UUID

import asyncpg
from pydantic import BaseModel

from agentzaaheen.core.sdk import Action, Event, Finding, RunRecord
from agentzaaheen.core.store.db import Scoped, require_aware, written

MAX_PAGE = 500

_EVENT_COLUMNS = (
    "id", "tenant_id", "project_id", "type", "source", "vendor_event_id", "idempotency_key",
    "occurred_at", "received_at", "schema_version", "payload", "trust", "data_class", "subject_ref",
)  # fmt: skip
_RUN_COLUMNS = (
    "id", "tenant_id", "project_id", "agent", "agent_version", "trigger_event_ids", "prompt_hash", "model",
    "data_class_max", "trust", "steps", "tokens_in", "tokens_out", "cost_minor_units", "started_at",
    "duration_ms", "outcome", "error_code",
)  # fmt: skip
_FINDING_COLUMNS = (
    "id", "tenant_id", "project_id", "run_id", "kind", "severity", "title", "body", "evidence",
    "metric_refs", "trust", "data_class", "status",
)  # fmt: skip
_ACTION_COLUMNS = (
    "id", "tenant_id", "project_id", "run_id", "tool", "level", "payload", "target_kind", "target_ref",
    "data_classes", "leak_check_passed", "idempotency_key", "status", "approved_by", "approved_at",
)  # fmt: skip

_INSERT_EVENT = """
INSERT INTO agentzaaheen.events (id, tenant_id, project_id, type, source, vendor_event_id, idempotency_key,
    occurred_at, received_at, schema_version, payload, trust, data_class, subject_ref)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
RETURNING id
"""
_SELECT_EVENT = """
SELECT id, tenant_id, project_id, type, source, vendor_event_id, idempotency_key, occurred_at, received_at,
    schema_version, payload, trust, data_class, subject_ref
FROM agentzaaheen.events WHERE tenant_id = $1 AND project_id = $2 AND id = $3
"""
_RECENT_EVENTS = """
SELECT id, tenant_id, project_id, type, source, vendor_event_id, idempotency_key, occurred_at, received_at,
    schema_version, payload, trust, data_class, subject_ref
FROM agentzaaheen.events WHERE tenant_id = $1 AND project_id = $2
ORDER BY received_at DESC, id LIMIT $3
"""
_INSERT_RUN = """
INSERT INTO agentzaaheen.runs (id, tenant_id, project_id, agent, agent_version, trigger_event_ids,
    prompt_hash,
    model, data_class_max, trust, steps, tokens_in, tokens_out, cost_minor_units, started_at, duration_ms,
    outcome, error_code)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18)
"""
_SELECT_RUN = """
SELECT id, tenant_id, project_id, agent, agent_version, trigger_event_ids, prompt_hash, model, data_class_max,
    trust, steps, tokens_in, tokens_out, cost_minor_units, started_at, duration_ms, outcome, error_code
FROM agentzaaheen.runs WHERE tenant_id = $1 AND project_id = $2 AND id = $3
"""
_INSERT_FINDING = """
INSERT INTO agentzaaheen.findings (id, tenant_id, project_id, run_id, kind, severity, title, body, evidence,
    metric_refs, trust, data_class, status)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
"""
_SELECT_FINDING = """
SELECT id, tenant_id, project_id, run_id, kind, severity, title, body, evidence, metric_refs, trust,
    data_class, status
FROM agentzaaheen.findings WHERE tenant_id = $1 AND project_id = $2 AND id = $3
"""
_INSERT_ACTION = """
INSERT INTO agentzaaheen.actions (id, tenant_id, project_id, run_id, tool, level, payload, target_kind,
    target_ref,
    data_classes, leak_check_passed, idempotency_key, status, approved_by, approved_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
RETURNING id
"""
_SELECT_ACTION = """
SELECT id, tenant_id, project_id, run_id, tool, level, payload, target_kind, target_ref, data_classes,
    leak_check_passed, idempotency_key, status, approved_by, approved_at
FROM agentzaaheen.actions WHERE tenant_id = $1 AND project_id = $2 AND id = $3
"""


def _plain(value: Any) -> Any:  # noqa: ANN401  # any field value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    return value


def _values(record: BaseModel, columns: Sequence[str], json_columns: frozenset[str]) -> list[Any]:
    """Column values in order. JSON columns use the model's JSON form; the rest stay typed."""
    typed = record.model_dump()
    as_json = record.model_dump(mode="json", include=set(json_columns))
    return [as_json[c] if c in json_columns else _plain(typed[c]) for c in columns]


def _load[M: BaseModel](model: type[M], row: asyncpg.Record | None) -> M | None:
    return None if row is None else model.model_validate(dict(row))


class Records:
    """Repository for the four SDK record types, in the one workspace of its handle.

    A record that names another workspace or tenant is refused by the database
    wall when it is saved; ids looked up elsewhere are simply not found.
    """

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope
        self._key = (scope.tenant_id, scope.require_workspace())

    async def add_event(self, event: Event) -> bool:
        """Store an event once. Returns False if its idempotency key was already stored."""
        values = _values(event, _EVENT_COLUMNS, frozenset({"payload"}))
        async with self._scope.write("an event") as conn:
            return written(await conn.fetchval(_INSERT_EVENT, *values))

    async def get_event(self, event_id: UUID) -> Event | None:
        """One event, or None if it does not exist in this workspace."""
        async with self._scope.connection() as conn:
            return _load(Event, await conn.fetchrow(_SELECT_EVENT, *self._key, event_id))

    async def recent_events(self, limit: int = 50) -> list[Event]:
        """Newest events in this workspace, at most ``MAX_PAGE``."""
        bounded = max(1, min(limit, MAX_PAGE))
        async with self._scope.connection() as conn:
            rows = await conn.fetch(_RECENT_EVENTS, *self._key, bounded)
        return [Event.model_validate(dict(row)) for row in rows]

    async def add_run(self, run: RunRecord) -> None:
        """Store one run record."""
        require_aware(run.started_at)
        values = _values(run, _RUN_COLUMNS, frozenset({"model"}))
        async with self._scope.write("an agent run") as conn:
            await conn.execute(_INSERT_RUN, *values)

    async def end_run(self, run: RunRecord, findings: Sequence[Finding]) -> None:
        """Store a run's end and its findings together: all of it is saved, or none of it (F11)."""
        require_aware(run.started_at)
        if any(f.run_id != run.id for f in findings):
            raise ValueError("every finding must name the run it came from")
        values = _values(run, _RUN_COLUMNS, frozenset({"model"}))
        rows = [_values(f, _FINDING_COLUMNS, frozenset({"evidence", "metric_refs"})) for f in findings]
        async with self._scope.write("an agent run's end and its findings") as conn:
            await conn.execute(_INSERT_RUN, *values)
            for row in rows:
                await conn.execute(_INSERT_FINDING, *row)

    async def get_run(self, run_id: UUID) -> RunRecord | None:
        """One run record, or None."""
        async with self._scope.connection() as conn:
            return _load(RunRecord, await conn.fetchrow(_SELECT_RUN, *self._key, run_id))

    async def add_finding(self, finding: Finding) -> None:
        """Store one finding."""
        values = _values(finding, _FINDING_COLUMNS, frozenset({"evidence", "metric_refs"}))
        async with self._scope.write("a finding") as conn:
            await conn.execute(_INSERT_FINDING, *values)

    async def get_finding(self, finding_id: UUID) -> Finding | None:
        """One finding, or None."""
        async with self._scope.connection() as conn:
            return _load(Finding, await conn.fetchrow(_SELECT_FINDING, *self._key, finding_id))

    async def add_action(self, action: Action) -> bool:
        """Store a proposed action once. Returns False if its idempotency key was already stored."""
        values = _values(action, _ACTION_COLUMNS, frozenset({"payload"}))
        async with self._scope.write("a proposed action") as conn:
            return written(await conn.fetchval(_INSERT_ACTION, *values))

    async def get_action(self, action_id: UUID) -> Action | None:
        """One action, or None."""
        async with self._scope.connection() as conn:
            return _load(Action, await conn.fetchrow(_SELECT_ACTION, *self._key, action_id))

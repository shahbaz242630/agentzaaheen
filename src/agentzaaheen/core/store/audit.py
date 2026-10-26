"""Audit rows: append under a per-tenant lock, read back in order. Never update or delete.

The store does not compute hashes (that is the policy layer's job, and the store
may not import it). It only guarantees order: :func:`AuditRows.append` takes a
transaction-scoped lock for the tenant, hands the caller the current head, and
inserts the row the caller built from it. The database also refuses any row that
does not link to the previous one, and any UPDATE, DELETE or TRUNCATE.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import asyncpg

from agentzaaheen.core.store.db import Scoped, require_aware

PAGE_SIZE = 1000

_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('agentzaaheen.audit:' || $1::uuid::text, 0))"
_HEAD = """
SELECT tenant_id, seq, at, actor, action, outcome, reason_code, correlation_id, project_id, target_ref,
    details, prev_hash, hash
FROM agentzaaheen.audit_log WHERE tenant_id = $1 ORDER BY seq DESC LIMIT 1
"""
_PAGE = """
SELECT tenant_id, seq, at, actor, action, outcome, reason_code, correlation_id, project_id, target_ref,
    details, prev_hash, hash
FROM agentzaaheen.audit_log WHERE tenant_id = $1 AND seq > $2 ORDER BY seq LIMIT $3
"""
# Runs the runner did not start because they were not meant to (F11): counted for the daily summary.
_SKIPPED = """
SELECT reason_code, details->>'role' AS role FROM agentzaaheen.audit_log
WHERE tenant_id = $1 AND project_id = $2 AND action = 'run.start' AND outcome = 'deny'
    AND reason_code = ANY($3::text[]) AND at >= $4 AND at < $5
ORDER BY seq
LIMIT $6
"""
_INSERT = """
INSERT INTO agentzaaheen.audit_log (tenant_id, seq, at, actor, action, outcome, reason_code, correlation_id,
    project_id, target_ref, details, prev_hash, hash)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
"""


@dataclass(frozen=True)
class AuditRow:
    """One stored audit row, exactly as the table holds it."""

    tenant_id: UUID
    seq: int
    at: datetime
    actor: str
    action: str
    outcome: str
    reason_code: str
    correlation_id: UUID
    project_id: UUID | None
    target_ref: str | None
    details: dict[str, str | int | bool | None]
    prev_hash: str
    hash: str


def _row(record: asyncpg.Record | None) -> AuditRow | None:
    return None if record is None else AuditRow(**dict(record))


def _values(row: AuditRow) -> tuple[object, ...]:
    return (
        row.tenant_id, row.seq, row.at, row.actor, row.action, row.outcome, row.reason_code,
        row.correlation_id, row.project_id, row.target_ref, row.details, row.prev_hash, row.hash,
    )  # fmt: skip


class AuditRows:
    """Repository for the append-only audit table."""

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope
        self._tenant = scope.tenant_id

    async def append(self, build: Callable[[AuditRow | None], AuditRow]) -> AuditRow:
        """Lock the tenant's chain, pass the head to ``build``, insert what it returns."""
        async with self._scope.write("an audit log entry") as conn:
            await conn.execute(_LOCK, self._tenant)
            head = _row(await conn.fetchrow(_HEAD, self._tenant))
            row = build(head)
            if row.tenant_id != self._tenant:
                raise ValueError("an audit row must belong to the tenant whose chain was locked")
            require_aware(row.at)
            await conn.execute(_INSERT, *_values(row))
            return row

    async def head(self) -> AuditRow | None:
        """The newest row of a tenant's chain, or None if it is empty."""
        async with self._scope.connection() as conn:
            return _row(await conn.fetchrow(_HEAD, self._tenant))

    async def skipped_runs(  # which workspace, which reasons, the window, how many
        self, project_id: UUID, reasons: Sequence[str], start: datetime, end: datetime, *, limit: int = 5000
    ) -> list[tuple[str, str | None]]:
        """``(reason, role)`` of each run not started for one of ``reasons`` in ``[start, end)``."""
        require_aware(start, end)
        async with self._scope.connection() as conn:
            rows = await conn.fetch(_SKIPPED, self._tenant, project_id, list(reasons), start, end, limit)
        return [(r["reason_code"], r["role"]) for r in rows]

    async def all_rows(self) -> list[AuditRow]:
        """Every row of a tenant's chain, oldest first, read page by page."""
        rows: list[AuditRow] = []
        async with self._scope.connection() as conn:
            while True:
                after = rows[-1].seq if rows else 0
                page = await conn.fetch(_PAGE, self._tenant, after, PAGE_SIZE)
                rows.extend(AuditRow(**dict(record)) for record in page)
                if len(page) < PAGE_SIZE:
                    return rows

"""Audit rows: append under a per-tenant lock, read back in order. Never update or delete.

The store does not compute hashes (that is the policy layer's job, and the store
may not import it). It only guarantees order: :func:`AuditRows.append` takes a
transaction-scoped lock for the tenant, hands the caller the current head, and
inserts the row the caller built from it. The database also refuses any row that
does not link to the previous one, and any UPDATE, DELETE or TRUNCATE.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import asyncpg

from yaqzan.core.store.db import Database, require_aware

PAGE_SIZE = 1000

_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('yaqzan.audit:' || $1::uuid::text, 0))"
_HEAD = """
SELECT tenant_id, seq, at, actor, action, outcome, reason_code, correlation_id, project_id, target_ref,
    details, prev_hash, hash
FROM yaqzan.audit_log WHERE tenant_id = $1 ORDER BY seq DESC LIMIT 1
"""
_PAGE = """
SELECT tenant_id, seq, at, actor, action, outcome, reason_code, correlation_id, project_id, target_ref,
    details, prev_hash, hash
FROM yaqzan.audit_log WHERE tenant_id = $1 AND seq > $2 ORDER BY seq LIMIT $3
"""
_INSERT = """
INSERT INTO yaqzan.audit_log (tenant_id, seq, at, actor, action, outcome, reason_code, correlation_id,
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

    def __init__(self, db: Database) -> None:
        self._db = db

    async def append(self, tenant_id: UUID, build: Callable[[AuditRow | None], AuditRow]) -> AuditRow:
        """Lock the tenant's chain, pass the head to ``build``, insert what it returns."""
        async with self._db.write("an audit log entry", atomic=True) as conn:
            await conn.execute(_LOCK, tenant_id)
            head = _row(await conn.fetchrow(_HEAD, tenant_id))
            row = build(head)
            if row.tenant_id != tenant_id:
                raise ValueError("an audit row must belong to the tenant whose chain was locked")
            require_aware(row.at)
            await conn.execute(_INSERT, *_values(row))
            return row

    async def head(self, tenant_id: UUID) -> AuditRow | None:
        """The newest row of a tenant's chain, or None if it is empty."""
        async with self._db.connection() as conn:
            return _row(await conn.fetchrow(_HEAD, tenant_id))

    async def all_rows(self, tenant_id: UUID) -> list[AuditRow]:
        """Every row of a tenant's chain, oldest first, read page by page."""
        rows: list[AuditRow] = []
        async with self._db.connection() as conn:
            while True:
                after = rows[-1].seq if rows else 0
                page = await conn.fetch(_PAGE, tenant_id, after, PAGE_SIZE)
                rows.extend(AuditRow(**dict(record)) for record in page)
                if len(page) < PAGE_SIZE:
                    return rows

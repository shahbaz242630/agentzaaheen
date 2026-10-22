"""Save and load the revenue mirror (Subscription, Payment, Refund) and per-source sync state.

A sync's rows and its "last success" mark are written in one transaction, so a
half-saved sync can never look fresh. A subscription or refund row is only
replaced by vendor data at least as new as what is stored. Reads come back as
SDK models, so every rule the shapes enforce is checked again on the way out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import asyncpg

from yaqzan.core.sdk import BillingCycle, Payment, Refund, RevenueBatch, Subscription
from yaqzan.core.store.db import Connection, Database, require_aware
from yaqzan.core.store.errors import StoreError, StoreErrorCode

# A read that would return more rows than this is refused rather than silently cut short.
MAX_ROWS = 100_000

_UPSERT_SUBSCRIPTION = """
INSERT INTO yaqzan.revenue_subscriptions (tenant_id, project_id, source, provider_id, customer_ref, status,
    cycle_interval, cycle_frequency, currency, recurring_minor, started_at, canceled_at, trial_ends_at,
    vendor_updated_at, synced_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
ON CONFLICT (tenant_id, project_id, source, provider_id) DO UPDATE SET
    customer_ref = EXCLUDED.customer_ref, status = EXCLUDED.status, cycle_interval = EXCLUDED.cycle_interval,
    cycle_frequency = EXCLUDED.cycle_frequency, currency = EXCLUDED.currency,
    recurring_minor = EXCLUDED.recurring_minor, started_at = EXCLUDED.started_at,
    canceled_at = EXCLUDED.canceled_at, trial_ends_at = EXCLUDED.trial_ends_at,
    vendor_updated_at = EXCLUDED.vendor_updated_at, synced_at = EXCLUDED.synced_at
WHERE EXCLUDED.vendor_updated_at >= yaqzan.revenue_subscriptions.vendor_updated_at
"""
_UPSERT_PAYMENT = """
INSERT INTO yaqzan.revenue_payments (tenant_id, project_id, source, provider_id, transaction_ref,
    subscription_ref, customer_ref, status, currency, amount_minor, tax_minor, error_code, occurred_at,
    synced_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
ON CONFLICT (tenant_id, project_id, source, provider_id) DO UPDATE SET
    transaction_ref = EXCLUDED.transaction_ref, subscription_ref = EXCLUDED.subscription_ref,
    customer_ref = EXCLUDED.customer_ref, status = EXCLUDED.status, currency = EXCLUDED.currency,
    amount_minor = EXCLUDED.amount_minor, tax_minor = EXCLUDED.tax_minor, error_code = EXCLUDED.error_code,
    occurred_at = EXCLUDED.occurred_at, synced_at = EXCLUDED.synced_at
"""
_UPSERT_REFUND = """
INSERT INTO yaqzan.revenue_refunds (tenant_id, project_id, source, provider_id, kind, status, transaction_ref,
    subscription_ref, currency, amount_minor, tax_minor, created_at, vendor_updated_at, synced_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
ON CONFLICT (tenant_id, project_id, source, provider_id) DO UPDATE SET
    kind = EXCLUDED.kind, status = EXCLUDED.status, transaction_ref = EXCLUDED.transaction_ref,
    subscription_ref = EXCLUDED.subscription_ref, currency = EXCLUDED.currency,
    amount_minor = EXCLUDED.amount_minor, tax_minor = EXCLUDED.tax_minor, created_at = EXCLUDED.created_at,
    vendor_updated_at = EXCLUDED.vendor_updated_at, synced_at = EXCLUDED.synced_at
WHERE EXCLUDED.vendor_updated_at >= yaqzan.revenue_refunds.vendor_updated_at
"""
_MARK_ATTEMPT = """
INSERT INTO yaqzan.source_syncs (tenant_id, project_id, source, last_attempt_at)
VALUES ($1, $2, $3, $4)
ON CONFLICT (tenant_id, project_id, source) DO UPDATE SET last_attempt_at = EXCLUDED.last_attempt_at
"""
_MARK_SUCCESS = """
INSERT INTO yaqzan.source_syncs (tenant_id, project_id, source, last_attempt_at, last_success_at, cursor_at)
VALUES ($1, $2, $3, $4, $4, $5)
ON CONFLICT (tenant_id, project_id, source) DO UPDATE SET
    last_attempt_at = EXCLUDED.last_attempt_at, last_success_at = EXCLUDED.last_success_at,
    last_error_code = NULL, cursor_at = EXCLUDED.cursor_at
"""
_MARK_FAILURE = """
INSERT INTO yaqzan.source_syncs (tenant_id, project_id, source, last_attempt_at, last_error_code)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (tenant_id, project_id, source) DO UPDATE SET
    last_attempt_at = EXCLUDED.last_attempt_at, last_error_code = EXCLUDED.last_error_code
"""
_SYNC_STATE = """
SELECT last_attempt_at, last_success_at, last_error_code, cursor_at FROM yaqzan.source_syncs
WHERE tenant_id = $1 AND project_id = $2 AND source = $3
"""
_SUBSCRIPTIONS = """
SELECT source, provider_id, customer_ref, status, cycle_interval, cycle_frequency, currency, recurring_minor,
    started_at, canceled_at, trial_ends_at, vendor_updated_at
FROM yaqzan.revenue_subscriptions WHERE tenant_id = $1 AND project_id = $2 AND source = $3
ORDER BY provider_id LIMIT $4
"""
_PAYMENTS = """
SELECT source, provider_id, transaction_ref, subscription_ref, customer_ref, status, currency, amount_minor,
    tax_minor, error_code, occurred_at
FROM yaqzan.revenue_payments
WHERE tenant_id = $1 AND project_id = $2 AND source = $3 AND occurred_at >= $4 AND occurred_at < $5
ORDER BY occurred_at, provider_id LIMIT $6
"""
_REFUNDS = """
SELECT source, provider_id, kind, status, transaction_ref, subscription_ref, currency, amount_minor,
    tax_minor, created_at, vendor_updated_at
FROM yaqzan.revenue_refunds
WHERE tenant_id = $1 AND project_id = $2 AND source = $3 AND created_at >= $4 AND created_at < $5
ORDER BY created_at, provider_id LIMIT $6
"""


@dataclass(frozen=True)
class SyncState:
    """Where one collector stands for one project."""

    last_attempt_at: datetime
    last_success_at: datetime | None
    last_error_code: str | None
    cursor_at: datetime | None


def _bounded(rows: list[asyncpg.Record]) -> list[asyncpg.Record]:
    if len(rows) > MAX_ROWS:
        raise StoreError(StoreErrorCode.FAILED, f"more than {MAX_ROWS} rows; narrow the window")
    return rows


class RevenueRows:
    """Repository for the revenue mirror and the sync state of every collector."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def save_batch(
        self, tenant_id: UUID, project_id: UUID, batch: RevenueBatch, *, at: datetime
    ) -> None:
        """Upsert everything in ``batch`` and mark the sync successful, all in one transaction."""
        require_aware(at)
        async with self._db.write("a revenue batch", atomic=True) as conn:
            await self._write(conn, tenant_id, project_id, batch, at)
            await conn.execute(_MARK_SUCCESS, tenant_id, project_id, batch.source, at, batch.fetched_at)

    @staticmethod
    async def _write(
        conn: Connection, tenant_id: UUID, project_id: UUID, batch: RevenueBatch, at: datetime
    ) -> None:
        key = (tenant_id, project_id)
        subs = [
            (*key, s.source, s.provider_id, s.customer_ref, s.status.value, s.cycle.interval.value,
             s.cycle.frequency, s.currency, s.recurring_minor, s.started_at, s.canceled_at, s.trial_ends_at,
             s.vendor_updated_at, at)
            for s in batch.subscriptions
        ]  # fmt: skip
        pays = [
            (*key, p.source, p.provider_id, p.transaction_ref, p.subscription_ref, p.customer_ref,
             p.status.value, p.currency, p.amount_minor, p.tax_minor, p.error_code, p.occurred_at, at)
            for p in batch.payments
        ]  # fmt: skip
        refunds = [
            (*key, r.source, r.provider_id, r.kind.value, r.status.value, r.transaction_ref,
             r.subscription_ref, r.currency, r.amount_minor, r.tax_minor, r.created_at,
             r.vendor_updated_at, at)
            for r in batch.refunds
        ]  # fmt: skip
        for sql, rows in ((_UPSERT_SUBSCRIPTION, subs), (_UPSERT_PAYMENT, pays), (_UPSERT_REFUND, refunds)):
            if rows:
                await conn.executemany(sql, rows)

    async def mark_attempt(self, tenant_id: UUID, project_id: UUID, source: str, *, at: datetime) -> None:
        """Record that a sync started."""
        require_aware(at)
        async with self._db.write("a revenue sync attempt") as conn:
            await conn.execute(_MARK_ATTEMPT, tenant_id, project_id, source, at)

    async def mark_failure(
        self, tenant_id: UUID, project_id: UUID, source: str, *, at: datetime, error_code: str
    ) -> None:
        """Record that a sync failed and why. The last success (and so freshness) is untouched."""
        require_aware(at)
        async with self._db.write("a failed revenue sync") as conn:
            await conn.execute(_MARK_FAILURE, tenant_id, project_id, source, at, error_code)

    async def sync_state(self, tenant_id: UUID, project_id: UUID, source: str) -> SyncState | None:
        """The collector's state, or None if it has never run for this project."""
        async with self._db.connection() as conn:
            row = await conn.fetchrow(_SYNC_STATE, tenant_id, project_id, source)
        return None if row is None else SyncState(**dict(row))

    async def subscriptions(self, tenant_id: UUID, project_id: UUID, source: str) -> list[Subscription]:
        """Every stored subscription from ``source`` for this project."""
        async with self._db.connection() as conn:
            rows = _bounded(await conn.fetch(_SUBSCRIPTIONS, tenant_id, project_id, source, MAX_ROWS + 1))
        out: list[Subscription] = []
        for row in rows:
            data = dict(row)
            cycle = BillingCycle(interval=data.pop("cycle_interval"), frequency=data.pop("cycle_frequency"))
            out.append(Subscription.model_validate({**data, "cycle": cycle}))
        return out

    async def payments(  # tenant, project, source and a window
        self, tenant_id: UUID, project_id: UUID, source: str, start: datetime, end: datetime
    ) -> list[Payment]:
        """Stored payment attempts from ``source`` that happened in ``[start, end)``."""
        require_aware(start, end)
        async with self._db.connection() as conn:
            rows = _bounded(
                await conn.fetch(_PAYMENTS, tenant_id, project_id, source, start, end, MAX_ROWS + 1)
            )
        return [Payment.model_validate(dict(row)) for row in rows]

    async def refunds(  # tenant, project, source and a window
        self, tenant_id: UUID, project_id: UUID, source: str, start: datetime, end: datetime
    ) -> list[Refund]:
        """Stored refunds and chargebacks from ``source`` created in ``[start, end)``."""
        require_aware(start, end)
        async with self._db.connection() as conn:
            rows = _bounded(
                await conn.fetch(_REFUNDS, tenant_id, project_id, source, start, end, MAX_ROWS + 1)
            )
        return [Refund.model_validate(dict(row)) for row in rows]

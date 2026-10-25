"""Run one revenue collector, and build the numbers the dashboard shows from what it stored.

A sync never half-succeeds: the rows and the "last success" mark land in one
transaction, and a failure only records its reason code, so freshness keeps
counting from the last real success. Payments are re-read from a little
before the last cursor, because a vendor's clock and ours are never exact.

A failure is logged in plain English (the error catalogue): what broke, where,
which attempt, the HTTP status, why, and what to do. The most precise code
(``net.timeout`` rather than ``source.unavailable``) is the one stored.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from agentzaaheen.core import metrics
from agentzaaheen.core.metrics import CountAndAmounts, Freshness, Unknown
from agentzaaheen.core.sdk import (
    Payment,
    Refund,
    RevenueSource,
    SourceError,
    SourceErrorCode,
    Subscription,
    failed,
    get_logger,
    say,
    span,
)
from agentzaaheen.core.store import RevenueRows, Scoped, StoreError

CURSOR_OVERLAP = timedelta(hours=1)
_CRASHED = "collect.crashed"


@dataclass(frozen=True)
class SyncOutcome:
    """What one sync did. ``error_code`` is set exactly when ``ok`` is False."""

    ok: bool
    error_code: str | None = None
    subscriptions: int = 0
    payments: int = 0
    refunds: int = 0


async def sync_revenue(scope: Scoped, source: RevenueSource, *, now: datetime) -> SyncOutcome:
    """Fetch from ``source``, save, and record the result in the source's sync state.

    ``scope`` is the one workspace this sync works in, given by whoever started it.
    """
    attributes = {
        "agentzaaheen.source": source.source,
        "agentzaaheen.project": str(scope.require_workspace()),
    }
    with span(f"sync revenue {source.source}", attributes=attributes) as current:
        outcome = await _sync(scope, source, now=now)
        if outcome.error_code is not None:
            failed(current, outcome.error_code)
        else:
            current.set_attribute(
                "agentzaaheen.count", outcome.subscriptions + outcome.payments + outcome.refunds
            )
        return outcome


async def _sync(scope: Scoped, source: RevenueSource, *, now: datetime) -> SyncOutcome:
    rows = RevenueRows(scope)
    log = get_logger("agentzaaheen.collect", source=source.source, project_id=str(scope.workspace_id))
    state = await rows.sync_state(source.source)
    since = None if state is None or state.cursor_at is None else state.cursor_at - CURSOR_OVERLAP
    await rows.mark_attempt(source.source, at=now)
    stage = f"fetch from {source.source}"
    try:
        batch = await source.fetch(payments_since=since, now=now)
        if batch.source != source.source:
            raise SourceError(SourceErrorCode.BAD_RESPONSE, "the batch names another source")
        stage = "save to the database"
        await rows.save_batch(batch, at=now)
    except (SourceError, StoreError) as exc:
        code = exc.reason_code if isinstance(exc, SourceError) else exc.code.value
        await rows.mark_failure(source.source, at=now, error_code=code)
        say(log, "revenue_sync_failed", stage=stage, reason_code=code, source=source.source, **_where(exc))
        return SyncOutcome(ok=False, error_code=code)
    except Exception:
        # A bug, not an outage: record it so the tile goes grey, then fail loudly.
        await rows.mark_failure(source.source, at=now, error_code=_CRASHED)
        say(log, "revenue_sync_crashed", stage=stage, reason_code=_CRASHED, source=source.source)
        raise
    counts = (len(batch.subscriptions), len(batch.payments), len(batch.refunds))
    say(log, "revenue_synced", subscriptions=counts[0], payments=counts[1], refunds=counts[2])
    return SyncOutcome(True, None, *counts)


def _where(exc: SourceError | StoreError) -> dict[str, object]:
    """Where a source failed: the operation, the attempt (``3/4``) and the HTTP status, when known."""
    if not isinstance(exc, SourceError):
        return {}
    where: dict[str, object] = {}
    if exc.operation is not None:
        where["operation"] = exc.operation
    if exc.attempt is not None:
        where["attempt"] = f"{exc.attempt}/{exc.attempts}"
    if exc.status is not None:
        where["http_status"] = exc.status
    return where


@dataclass(frozen=True)
class RevenueSnapshot:
    """One project's revenue numbers from one source, or ``Unknown`` for each when stale."""

    source: str
    as_of: datetime
    freshness: Freshness
    last_success_at: datetime | None
    mrr: dict[str, int] | Unknown
    active_subscribers: int | Unknown
    trials: int | Unknown
    failed_payments: CountAndAmounts | Unknown
    refunds: CountAndAmounts | Unknown
    cash_revenue: dict[str, int] | Unknown
    cash_revenue_ex_tax: dict[str, int] | Unknown


async def revenue_snapshot(  # noqa: PLR0913  # where, which source, when, and the period
    scope: Scoped,
    source: str,
    *,
    now: datetime,
    schedule: timedelta,
    start: datetime,
    end: datetime,
) -> RevenueSnapshot:
    """Numbers for ``[start, end)`` from stored rows. Not fresh = every number unknown, never 0."""
    rows = RevenueRows(scope)
    state = await rows.sync_state(source)
    last_success = None if state is None else state.last_success_at
    if not metrics.freshness(last_success, schedule, now).usable:
        return snapshot_from_rows(
            source, (), (), (), now=now, last_success=last_success, schedule=schedule, start=start, end=end
        )
    return snapshot_from_rows(
        source,
        await rows.subscriptions(source),
        await rows.payments(source, start, end),
        await rows.refunds(source, start, end),
        now=now,
        last_success=last_success,
        schedule=schedule,
        start=start,
        end=end,
    )


def snapshot_from_rows(  # noqa: PLR0913  # the rows, plus when they were read and for which period
    source: str,
    subs: Sequence[Subscription],
    pays: Sequence[Payment],
    backs: Sequence[Refund],
    *,
    now: datetime,
    last_success: datetime | None,
    schedule: timedelta,
    start: datetime,
    end: datetime,
) -> RevenueSnapshot:
    """Numbers for ``[start, end)`` from rows already in hand. Not fresh = every number unknown, never 0."""
    fresh = metrics.freshness(last_success, schedule, now)
    if not fresh.usable:
        gone = Unknown(f"freshness.{fresh.value}")
        return RevenueSnapshot(source, now, fresh, last_success, gone, gone, gone, gone, gone, gone, gone)
    return RevenueSnapshot(
        source=source,
        as_of=now,
        freshness=fresh,
        last_success_at=last_success,
        mrr=metrics.mrr(subs),
        active_subscribers=metrics.active_subscribers(subs),
        trials=metrics.trials(subs),
        failed_payments=metrics.failed_payments(pays, start, end),
        refunds=metrics.refunds(backs, start, end),
        cash_revenue=metrics.cash_revenue(pays, backs, start, end, tax=True),
        cash_revenue_ex_tax=metrics.cash_revenue(pays, backs, start, end, tax=False),
    )

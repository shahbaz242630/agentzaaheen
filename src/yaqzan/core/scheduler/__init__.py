"""Clock and event triggers that wake collectors and agents (debounced, once-only catch-up).

What it does (so far):
    Runs one revenue collector (:func:`sync_revenue`): fetch, save, and record
    success or the failure reason; and builds a project's revenue numbers from
    stored rows (:func:`revenue_snapshot`), all ``Unknown`` when the data is
    stale. The clock and job queue arrive with the scheduler feature (ADR-018).

Public interface:
    ``sync_revenue``, ``SyncOutcome``, ``revenue_snapshot``,
    ``RevenueSnapshot``, ``CURSOR_OVERLAP``.

How to test:
    ``uv run python -m tools.dev_db up`` then
    ``uv run pytest tests/integration/test_scheduler_collect.py``

Must never:
    Run agent logic itself; mark a sync successful unless its rows are saved;
    show a stale number as if it were current.
"""

from yaqzan.core.scheduler.collect import (
    CURSOR_OVERLAP,
    RevenueSnapshot,
    SyncOutcome,
    revenue_snapshot,
    sync_revenue,
)

__all__ = ["CURSOR_OVERLAP", "RevenueSnapshot", "SyncOutcome", "revenue_snapshot", "sync_revenue"]

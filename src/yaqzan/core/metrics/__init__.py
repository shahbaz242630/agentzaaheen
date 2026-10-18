"""Metrics: every number Yaqzan shows, computed by code from stored data (07-METRICS).

What it does:
    Pure functions over the SDK's revenue shapes: MRR per currency
    (normalised to monthly, exact until one final rounding), active
    subscribers, trials, failed payments, refunds, cash revenue, the
    failed-payment spike rule, and the freshness rule (stale = unknown).

Public interface:
    Everything in ``__all__`` below. A number that cannot be given truthfully
    comes back as :class:`Unknown`, never as 0.

How to test:
    ``uv run pytest tests/unit/test_metrics_revenue.py tests/unit/test_metrics_freshness.py``

Must never:
    Use floats for money; add amounts across currencies; read the database or
    the network (callers pass the rows in); let an AI produce a number.
"""

from yaqzan.core.metrics.freshness import ALERT_AFTER, STALE_AFTER, Freshness, freshness
from yaqzan.core.metrics.revenue import (
    COUNTED_STATUSES,
    MIN_SPIKE_FAILURES,
    CountAndAmounts,
    Unknown,
    active_subscribers,
    cash_revenue,
    failed_payment_spike,
    failed_payments,
    monthly_share,
    mrr,
    refunds,
    trials,
)

__all__ = [
    "ALERT_AFTER",
    "COUNTED_STATUSES",
    "MIN_SPIKE_FAILURES",
    "STALE_AFTER",
    "CountAndAmounts",
    "Freshness",
    "Unknown",
    "active_subscribers",
    "cash_revenue",
    "failed_payment_spike",
    "failed_payments",
    "freshness",
    "monthly_share",
    "mrr",
    "refunds",
    "trials",
]

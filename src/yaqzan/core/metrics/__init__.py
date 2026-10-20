"""Metrics: every number Yaqzan shows, computed by code from stored data (07-METRICS).

What it does:
    Pure functions over the SDK's revenue shapes: MRR per currency
    (normalised to monthly, exact until one final rounding), active
    subscribers, trials, failed payments, refunds, cash revenue, the
    failed-payment spike rule, the freshness rule (stale = unknown), the
    key-expiry rule (warn 14 days ahead, P1 once expired or refused) and the
    SLO rules (compliance, error budget left, multi-window burn alerts).

Public interface:
    Everything in ``__all__`` below. A number that cannot be given truthfully
    comes back as :class:`Unknown`, never as 0.

How to test:
    ``uv run pytest tests/unit/test_metrics_revenue.py tests/unit/test_metrics_freshness.py``
    ``uv run pytest tests/unit/test_metrics_keys.py tests/unit/test_metrics_slo.py``

Must never:
    Use floats for money; add amounts across currencies; read the database or
    the network (callers pass the rows in); let an AI produce a number.
"""

from yaqzan.core.metrics.freshness import ALERT_AFTER, STALE_AFTER, Freshness, freshness
from yaqzan.core.metrics.keys import REFUSED_CODES, WARN_BEFORE, KeyHealth, days_left, key_health
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
from yaqzan.core.metrics.slo import (
    BURN_RULES,
    BURN_WINDOWS,
    BurnRule,
    Counts,
    Objective,
    SliKind,
    budget_left,
    burn_alert,
    burn_rate,
    compliance,
)

__all__ = [
    "ALERT_AFTER",
    "BURN_RULES",
    "BURN_WINDOWS",
    "COUNTED_STATUSES",
    "MIN_SPIKE_FAILURES",
    "REFUSED_CODES",
    "STALE_AFTER",
    "WARN_BEFORE",
    "BurnRule",
    "CountAndAmounts",
    "Counts",
    "Freshness",
    "KeyHealth",
    "Objective",
    "SliKind",
    "Unknown",
    "active_subscribers",
    "budget_left",
    "burn_alert",
    "burn_rate",
    "cash_revenue",
    "compliance",
    "days_left",
    "failed_payment_spike",
    "failed_payments",
    "freshness",
    "key_health",
    "monthly_share",
    "mrr",
    "refunds",
    "trials",
]

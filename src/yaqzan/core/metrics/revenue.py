"""Revenue metrics, exactly as ``07-METRICS`` §1 defines them. Pure code, no AI.

Amounts stay integer minor units per currency; nothing is converted or added
across currencies here. MRR is summed exactly (fractions) and rounded once,
half up, at the end, so ten annual plans never drift by a cent each.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction

from yaqzan.core.sdk import (
    BillingCycle,
    Interval,
    Payment,
    PaymentStatus,
    Refund,
    RefundKind,
    RefundStatus,
    Subscription,
    SubscriptionStatus,
)

# Subscribers we count: paying now, or in the grace period after a failed renewal.
COUNTED_STATUSES = frozenset({SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE})

# Months per one interval unit (weekly x 52 / 12, daily x 365 / 12).
_MONTHS_PER_UNIT: dict[Interval, Fraction] = {
    Interval.DAY: Fraction(12, 365),
    Interval.WEEK: Fraction(12, 52),
    Interval.MONTH: Fraction(1),
    Interval.YEAR: Fraction(12),
}


def _round_half_up(value: Fraction) -> int:
    whole, rest = divmod(value.numerator, value.denominator)
    return whole + (1 if 2 * rest >= value.denominator else 0)


def monthly_share(amount_minor: int, cycle: BillingCycle) -> Fraction:
    """What one cycle's amount is worth per month, exactly."""
    return Fraction(amount_minor) / (_MONTHS_PER_UNIT[cycle.interval] * cycle.frequency)


@dataclass(frozen=True)
class Unknown:
    """A number we cannot give truthfully. ``reason`` is a code, shown as "unknown"."""

    reason: str


@dataclass(frozen=True)
class CountAndAmounts:
    """How many, and how much per currency."""

    count: int = 0
    amounts: dict[str, int] = field(default_factory=dict)


def _in_window(moment: datetime, start: datetime, end: datetime) -> bool:
    return start <= moment < end


def mrr(subscriptions: Iterable[Subscription]) -> dict[str, int] | Unknown:
    """Gross MRR per currency: counted subscriptions' recurring price, normalised to monthly.

    Unknown if any counted subscription's price could not be worked out exactly:
    a partial total would look like a real drop in revenue.
    """
    totals: dict[str, Fraction] = {}
    for sub in subscriptions:
        if sub.status not in COUNTED_STATUSES:
            continue
        if sub.recurring_minor is None:
            return Unknown("mrr.price_not_exact")
        totals[sub.currency] = totals.get(sub.currency, Fraction(0)) + monthly_share(
            sub.recurring_minor, sub.cycle
        )
    return {currency: _round_half_up(total) for currency, total in sorted(totals.items())}


def active_subscribers(subscriptions: Iterable[Subscription]) -> int:
    """Distinct customers with at least one active or past-due subscription (trials excluded)."""
    return len({(s.source, s.customer_ref) for s in subscriptions if s.status in COUNTED_STATUSES})


def trials(subscriptions: Iterable[Subscription]) -> int:
    """Distinct customers on a trial, counted separately and never as subscribers."""
    return len({(s.source, s.customer_ref) for s in subscriptions if s.status is SubscriptionStatus.TRIALING})


def _add(totals: dict[str, int], currency: str, amount: int) -> None:
    totals[currency] = totals.get(currency, 0) + amount


def failed_payments(payments: Iterable[Payment], start: datetime, end: datetime) -> CountAndAmounts:
    """Payment attempts that failed in ``[start, end)``: count and amount per currency."""
    count, amounts = 0, dict[str, int]()
    for p in payments:
        if p.status is PaymentStatus.FAILED and _in_window(p.occurred_at, start, end):
            count += 1
            _add(amounts, p.currency, p.amount_minor)
    return CountAndAmounts(count, dict(sorted(amounts.items())))


def refunds(items: Iterable[Refund], start: datetime, end: datetime) -> CountAndAmounts:
    """Approved refunds and chargebacks created in ``[start, end)``.

    A chargeback reversal gives money back to us: it lowers the amount but
    does not un-count the chargeback.
    """
    count, amounts = 0, dict[str, int]()
    for r in items:
        if r.status is not RefundStatus.APPROVED or not _in_window(r.created_at, start, end):
            continue
        if r.kind is RefundKind.CHARGEBACK_REVERSAL:
            _add(amounts, r.currency, -r.amount_minor)
        else:
            count += 1
            _add(amounts, r.currency, r.amount_minor)
    return CountAndAmounts(count, dict(sorted(amounts.items())))


def cash_revenue(
    payments: Iterable[Payment], items: Iterable[Refund], start: datetime, end: datetime, *, tax: bool
) -> dict[str, int]:
    """Successful payments in the window minus approved refunds, per currency.

    ``tax=True`` keeps tax in (what customers paid); ``False`` takes it out.
    """
    totals: dict[str, int] = {}
    for p in payments:
        if p.status is PaymentStatus.SUCCEEDED and _in_window(p.occurred_at, start, end):
            _add(totals, p.currency, p.amount_minor - (0 if tax else p.tax_minor))
    for r in items:
        if r.status is RefundStatus.APPROVED and _in_window(r.created_at, start, end):
            value = r.amount_minor - (0 if tax else r.tax_minor)
            _add(totals, r.currency, value if r.kind is RefundKind.CHARGEBACK_REVERSAL else -value)
    return dict(sorted(totals.items()))


# A spike needs at least this many failures, so one failed card on a quiet day is not a P1.
MIN_SPIKE_FAILURES = 3
SPIKE_FACTOR = 3
SPIKE_HISTORY_DAYS = 14


def failed_payment_spike(previous_days: Sequence[int], today: int) -> bool:
    """METRICS §1: failures today above 3x the 14-day average = P1.

    Needs a full 14 days of history (otherwise there is no average to beat)
    and at least ``MIN_SPIKE_FAILURES`` failures today.
    """
    if len(previous_days) != SPIKE_HISTORY_DAYS or any(day < 0 for day in previous_days) or today < 0:
        raise ValueError("needs exactly 14 non-negative daily counts and a non-negative today")
    if today < MIN_SPIKE_FAILURES:
        return False
    return today * SPIKE_HISTORY_DAYS > SPIKE_FACTOR * sum(previous_days)

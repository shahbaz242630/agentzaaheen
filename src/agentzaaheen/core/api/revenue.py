"""Turn a stored revenue snapshot into a dashboard card (F04 slice 2).

The numbers are computed by ``core.metrics`` and stored by the scheduler; this
module only reshapes them. A snapshot that is not fresh already holds
``Unknown`` for every number, and the card keeps it that way.
"""

from __future__ import annotations

from agentzaaheen.core.api.views import Card, Count, Metric, Money, NotKnown
from agentzaaheen.core.metrics import CountAndAmounts, Unknown
from agentzaaheen.core.scheduler import RevenueSnapshot


def revenue_card(  # noqa: PLR0913  # which card, what to call it, and how to read the period
    snapshot: RevenueSnapshot,
    *,
    card_id: str,
    title: str,
    source_label: str,
    home_currency: str,
    period_label: str,
) -> Card:
    """One source's revenue card. ``home_currency`` labels an MRR of zero (no subscribers yet)."""
    metrics = (
        *_money("MRR", snapshot.mrr, home_currency),
        _count("Active subscribers", snapshot.active_subscribers),
        _count("Trials", snapshot.trials),
        _events("Failed payments", snapshot.failed_payments, period_label),
        _events("Refunds", snapshot.refunds, period_label),
        *_money("Revenue excl. tax", snapshot.cash_revenue_ex_tax, home_currency, period_label),
    )
    return Card(
        id=card_id,
        title=title,
        source=source_label,
        freshness=snapshot.freshness,
        last_success_at=snapshot.last_success_at,
        sample=False,
        metrics=metrics,
    )


def _unknown(label: str, value: Unknown, detail: str | None = None) -> Metric:
    return Metric(label=label, value=NotKnown(reason=value.reason), detail=detail)


def _count(label: str, value: int | Unknown) -> Metric:
    if isinstance(value, Unknown):
        return _unknown(label, value)
    return Metric(label=label, value=Count(value=value))


def _events(label: str, value: CountAndAmounts | Unknown, period: str) -> Metric:
    if isinstance(value, Unknown):
        return _unknown(label, value, period)
    return Metric(label=label, value=Count(value=value.count), detail=period)


def _money(
    label: str, value: dict[str, int] | Unknown, home: str, detail: str | None = None
) -> tuple[Metric, ...]:
    """One metric per currency (amounts in different currencies are never added together)."""
    if isinstance(value, Unknown):
        return (_unknown(label, value, detail),)
    if not value:
        return (Metric(label=label, value=Money(minor=0, currency=home), detail=detail),)
    if len(value) == 1:
        (currency, minor), *_ = value.items()
        return (Metric(label=label, value=Money(minor=minor, currency=currency), detail=detail),)
    order = sorted(value, key=lambda currency: (currency != home, currency))
    return tuple(
        Metric(
            label=f"{label} ({currency})",
            value=Money(minor=value[currency], currency=currency),
            detail=detail,
        )
        for currency in order
    )

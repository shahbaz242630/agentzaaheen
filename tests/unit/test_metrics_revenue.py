"""Revenue metrics follow 07-METRICS §1 exactly, with integer money and honest "unknown"."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.fixtures import paddle_world as world
from yaqzan.core import metrics
from yaqzan.core.metrics import CountAndAmounts, Unknown
from yaqzan.core.sdk import BillingCycle, Interval, PaymentStatus, Subscription, SubscriptionStatus

START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 11, 1, tzinfo=UTC)


def sub(
    n: int,
    status: SubscriptionStatus,
    recurring: int | None,
    *,
    customer: int | None = None,
    interval: Interval = Interval.MONTH,
    frequency: int = 1,
    currency: str = "USD",
) -> Subscription:
    return Subscription(
        source="paddle",
        provider_id=f"sub_{n}",
        customer_ref=f"ctm_{customer if customer is not None else n}",
        status=status,
        cycle=BillingCycle(interval=interval, frequency=frequency),
        currency=currency,
        recurring_minor=recurring,
        vendor_updated_at=START,
    )


# ---------------------------------------------------------------- the synthetic Paddle account


def test_world_numbers() -> None:
    subs = world.EXPECTED_SUBSCRIPTIONS
    # $5 monthly + $48 yearly ($4/month) + past-due $5 at 20% off ($4); trial, canceled, paused excluded.
    assert metrics.mrr(subs) == {"GBP": 300, "USD": 1300}
    assert metrics.active_subscribers(subs) == 3  # customers 1 (twice), 2 and 5
    assert metrics.trials(subs) == 1
    assert metrics.failed_payments(world.EXPECTED_PAYMENTS, START, END) == CountAndAmounts(2, {"USD": 945})
    # refund 5040 + chargeback 525 - reversal 525; the pending refund is not money moved.
    assert metrics.refunds(world.EXPECTED_REFUNDS, START, END) == CountAndAmounts(2, {"USD": 5040})
    pays, refs = world.EXPECTED_PAYMENTS, world.EXPECTED_REFUNDS
    assert metrics.cash_revenue(pays, refs, START, END, tax=True) == {"GBP": 360, "USD": 525}
    assert metrics.cash_revenue(pays, refs, START, END, tax=False) == {"GBP": 300, "USD": 500}


# ---------------------------------------------------------------- MRR


@pytest.mark.parametrize(
    ("interval", "frequency", "amount", "monthly"),
    [
        (Interval.MONTH, 1, 500, 500),
        (Interval.MONTH, 3, 1500, 500),
        (Interval.YEAR, 1, 4800, 400),
        (Interval.YEAR, 2, 4800, 200),
        (Interval.WEEK, 1, 100, 433),  # 100 x 52 / 12 = 433.33
        (Interval.DAY, 1, 10, 304),  # 10 x 365 / 12 = 304.17
        (Interval.YEAR, 1, 1000, 83),  # 83.33
        (Interval.YEAR, 1, 1002, 84),  # 83.5 rounds half up
    ],
)
def test_mrr_normalises_to_monthly(interval: Interval, frequency: int, amount: int, monthly: int) -> None:
    assert metrics.mrr(
        [sub(1, SubscriptionStatus.ACTIVE, amount, interval=interval, frequency=frequency)]
    ) == {"USD": monthly}


def test_mrr_rounds_once_at_the_end_not_per_subscription() -> None:
    # Three annual plans of 1000: 83.33 each. Rounded each, 249; summed exactly, 250.
    subs = [sub(n, SubscriptionStatus.ACTIVE, 1000, interval=Interval.YEAR) for n in range(3)]
    assert metrics.mrr(subs) == {"USD": 250}


def test_mrr_never_adds_currencies_together() -> None:
    subs = [sub(1, SubscriptionStatus.ACTIVE, 500), sub(2, SubscriptionStatus.ACTIVE, 400, currency="EUR")]
    assert metrics.mrr(subs) == {"EUR": 400, "USD": 500}


def test_mrr_is_unknown_when_a_counted_price_is_unknown() -> None:
    subs = [sub(1, SubscriptionStatus.ACTIVE, 500), sub(2, SubscriptionStatus.PAST_DUE, None)]
    assert metrics.mrr(subs) == Unknown("mrr.price_not_exact")


def test_an_unknown_price_outside_mrr_does_not_poison_it() -> None:
    subs = [
        sub(1, SubscriptionStatus.ACTIVE, 500),
        sub(2, SubscriptionStatus.TRIALING, None),
        sub(3, SubscriptionStatus.CANCELED, None),
    ]
    assert metrics.mrr(subs) == {"USD": 500}


def test_no_subscriptions_is_zero_mrr_not_unknown() -> None:
    assert metrics.mrr([]) == {}


@given(
    st.lists(
        st.tuples(st.integers(0, 10**9), st.sampled_from(list(Interval)), st.integers(1, 12)), max_size=30
    )
)
def test_mrr_is_the_exact_sum_rounded_once(plans: list[tuple[int, Interval, int]]) -> None:
    subs = [
        sub(n, SubscriptionStatus.ACTIVE, a, interval=i, frequency=f) for n, (a, i, f) in enumerate(plans)
    ]
    exact = sum(
        (metrics.monthly_share(a, BillingCycle(interval=i, frequency=f)) for a, i, f in plans), Fraction(0)
    )
    result = metrics.mrr(subs)
    assert isinstance(result, dict)
    if plans:
        assert abs(Fraction(result["USD"]) - exact) <= Fraction(1, 2)
    else:
        assert result == {}


# ---------------------------------------------------------------- subscribers and trials


def test_subscribers_are_distinct_customers_and_trials_are_separate() -> None:
    subs = [
        sub(1, SubscriptionStatus.ACTIVE, 500, customer=1),
        sub(2, SubscriptionStatus.PAST_DUE, 500, customer=1),
        sub(3, SubscriptionStatus.TRIALING, 500, customer=2),
        sub(4, SubscriptionStatus.TRIALING, 500, customer=2),
        sub(5, SubscriptionStatus.PAUSED, 500, customer=3),
        sub(6, SubscriptionStatus.INCOMPLETE, 500, customer=4),
        sub(7, SubscriptionStatus.CANCELED, 500, customer=5),
    ]
    assert metrics.active_subscribers(subs) == 1
    assert metrics.trials(subs) == 1


# ---------------------------------------------------------------- windows


def test_windows_include_start_and_exclude_end() -> None:
    pays = [
        p.model_copy(update={"occurred_at": t})
        for p, t in zip(world.EXPECTED_PAYMENTS[:2], (START, END), strict=True)
    ]
    failed = [p.model_copy(update={"status": PaymentStatus.FAILED}) for p in pays]
    assert metrics.failed_payments(failed, START, END).count == 1
    assert metrics.failed_payments(failed, START + timedelta(microseconds=1), END).count == 0


def test_refunds_outside_the_window_are_ignored() -> None:
    later = datetime(2026, 12, 1, tzinfo=UTC)
    assert metrics.refunds(world.EXPECTED_REFUNDS, later, later + timedelta(days=1)) == CountAndAmounts()
    assert (
        metrics.cash_revenue(
            world.EXPECTED_PAYMENTS, world.EXPECTED_REFUNDS, later, later + timedelta(days=1), tax=True
        )
        == {}
    )


# ---------------------------------------------------------------- the failed-payment spike rule (P1)


@pytest.mark.parametrize(
    ("history", "today", "spike"),
    [
        ([1] * 14, 4, True),  # 4 > 3 x 1
        ([1] * 14, 3, False),  # exactly 3x is not above
        ([0] * 14, 3, True),
        ([0] * 14, 2, False),  # below the minimum count
        ([10] * 14, 31, True),
        ([10] * 14, 30, False),
        ([0] * 13 + [14], 4, True),  # average 1
    ],
)
def test_spike_rule(history: list[int], today: int, spike: bool) -> None:
    assert metrics.failed_payment_spike(history, today) is spike


@pytest.mark.parametrize(
    ("history", "today"), [([1] * 13, 5), ([1] * 15, 5), ([-1] + [1] * 13, 5), ([1] * 14, -1)]
)
def test_spike_rule_refuses_bad_history(history: list[int], today: int) -> None:
    with pytest.raises(ValueError, match="14 non-negative"):
        metrics.failed_payment_spike(history, today)

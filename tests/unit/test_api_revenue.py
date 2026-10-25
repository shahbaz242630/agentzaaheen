from datetime import UTC, datetime, timedelta

import pytest

from agentzaaheen.core.api.revenue import revenue_card
from agentzaaheen.core.api.views import Count, Money, NotKnown
from agentzaaheen.core.metrics import CountAndAmounts, Freshness, Unknown
from agentzaaheen.core.scheduler import RevenueSnapshot

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def _fresh(**changes: object) -> RevenueSnapshot:
    values: dict[str, object] = {
        "source": "paddle",
        "as_of": NOW,
        "freshness": Freshness.FRESH,
        "last_success_at": NOW - timedelta(minutes=12),
        "mrr": {"USD": 124_000},
        "active_subscribers": 87,
        "trials": 14,
        "failed_payments": CountAndAmounts(count=2, amounts={"USD": 5_800}),
        "refunds": CountAndAmounts(count=1, amounts={"USD": 1_900}),
        "cash_revenue": {"USD": 130_000},
        "cash_revenue_ex_tax": {"USD": 120_000},
    }
    values.update(changes)
    return RevenueSnapshot(**values)  # type: ignore[arg-type]


def _card(snapshot: RevenueSnapshot) -> dict[str, object]:
    card = revenue_card(
        snapshot,
        card_id="paddle",
        title="Revenue",
        source_label="Paddle",
        home_currency="USD",
        period_label="Last 30 days",
    )
    assert card.sample is False
    assert card.freshness is snapshot.freshness
    assert card.last_success_at == snapshot.last_success_at
    return {m.label: m for m in card.metrics}


def test_a_fresh_snapshot_shows_its_numbers() -> None:
    by_label = _card(_fresh())
    assert list(by_label) == [
        "MRR",
        "Active subscribers",
        "Trials",
        "Failed payments",
        "Refunds",
        "Revenue excl. tax",
    ]
    assert by_label["MRR"].value == Money(minor=124_000, currency="USD")  # type: ignore[attr-defined]
    assert by_label["Active subscribers"].value == Count(value=87)  # type: ignore[attr-defined]
    assert by_label["Failed payments"].value == Count(value=2)  # type: ignore[attr-defined]
    assert by_label["Failed payments"].detail == "Last 30 days"  # type: ignore[attr-defined]
    assert by_label["Revenue excl. tax"].value == Money(minor=120_000, currency="USD")  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("state", "last"),
    [
        (Freshness.STALE, NOW - timedelta(days=1)),
        (Freshness.ALERT, NOW - timedelta(days=9)),
        (Freshness.NEVER, None),
    ],
)
def test_a_snapshot_that_is_not_fresh_shows_only_unknowns(state: Freshness, last: datetime | None) -> None:
    gone = Unknown(f"freshness.{state.value}")
    snapshot = _fresh(
        freshness=state,
        last_success_at=last,
        mrr=gone,
        active_subscribers=gone,
        trials=gone,
        failed_payments=gone,
        refunds=gone,
        cash_revenue=gone,
        cash_revenue_ex_tax=gone,
    )
    by_label = _card(snapshot)
    assert len(by_label) == 6
    for metric in by_label.values():
        assert metric.value == NotKnown(reason=f"freshness.{state.value}")  # type: ignore[attr-defined]


def test_no_subscribers_yet_is_a_real_zero_in_the_home_currency() -> None:
    by_label = _card(_fresh(mrr={}, cash_revenue_ex_tax={}))
    assert by_label["MRR"].value == Money(minor=0, currency="USD")  # type: ignore[attr-defined]
    assert by_label["Revenue excl. tax"].value == Money(minor=0, currency="USD")  # type: ignore[attr-defined]


def test_currencies_are_never_added_together() -> None:
    by_label = _card(_fresh(mrr={"EUR": 5_000, "USD": 124_000, "GBP": 900}))
    assert [label for label in by_label if label.startswith("MRR")] == ["MRR (USD)", "MRR (EUR)", "MRR (GBP)"]
    assert by_label["MRR (EUR)"].value == Money(minor=5_000, currency="EUR")  # type: ignore[attr-defined]


def test_an_mrr_that_cannot_be_worked_out_stays_unknown_while_the_rest_shows() -> None:
    by_label = _card(_fresh(mrr=Unknown("mrr.price_not_exact")))
    assert by_label["MRR"].value == NotKnown(reason="mrr.price_not_exact")  # type: ignore[attr-defined]
    assert by_label["Trials"].value == Count(value=14)  # type: ignore[attr-defined]

"""SLO rules (ADR-032): compliance, budget left, burn rate, multi-window burn alerts."""

from __future__ import annotations

from datetime import timedelta
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from yaqzan.core.metrics import (
    BURN_RULES,
    BURN_WINDOWS,
    Counts,
    Objective,
    SliKind,
    Unknown,
    budget_left,
    burn_alert,
    burn_rate,
    compliance,
)
from yaqzan.core.sdk import Severity

T995 = Fraction(995, 1000)
DAYS28 = timedelta(days=28)
H1, M5, H6, M30, D3 = (
    timedelta(hours=1),
    timedelta(minutes=5),
    timedelta(hours=6),
    timedelta(minutes=30),
    timedelta(days=3),
)


def _objective(**changes: object) -> Objective:
    fields: dict[str, object] = {
        "project": "sandoq-kin",
        "name": "api-availability",
        "kind": SliKind.AVAILABILITY,
        "target": T995,
        "window": DAYS28,
    }
    fields.update(changes)
    return Objective(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------- objectives


def test_budget_and_allowed_downtime_are_exact() -> None:
    objective = _objective()
    assert objective.budget == Fraction(5, 1000)
    assert objective.allowed_downtime == timedelta(hours=3, minutes=21, seconds=36)  # 0.5% of 28 days


@pytest.mark.parametrize("target", [Fraction(0), Fraction(1), Fraction(-1, 10), Fraction(11, 10)])
def test_target_must_be_strictly_between_0_and_1(target: Fraction) -> None:
    with pytest.raises(ValueError, match="target"):
        _objective(target=target)


@pytest.mark.parametrize("window", [timedelta(hours=23), timedelta(days=91), timedelta(0)])
def test_window_must_be_1_to_90_days(window: timedelta) -> None:
    with pytest.raises(ValueError, match="window"):
        _objective(window=window)


def test_window_edges_are_allowed() -> None:
    assert _objective(window=timedelta(days=1)).window == timedelta(days=1)
    assert _objective(window=timedelta(days=90)).window == timedelta(days=90)


def test_latency_needs_a_threshold_and_only_latency_has_one() -> None:
    assert _objective(kind=SliKind.LATENCY, threshold_ms=800).threshold_ms == 800
    with pytest.raises(ValueError, match="threshold"):
        _objective(kind=SliKind.LATENCY)
    with pytest.raises(ValueError, match="threshold"):
        _objective(kind=SliKind.ERRORS, threshold_ms=800)


@pytest.mark.parametrize("ms", [0, -5])
def test_latency_threshold_must_be_positive(ms: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        _objective(kind=SliKind.LATENCY, threshold_ms=ms)


@pytest.mark.parametrize(("good", "total"), [(-1, 5), (6, 5), (0, -1)])
def test_counts_must_be_consistent(good: int, total: int) -> None:
    with pytest.raises(ValueError, match="good events"):
        Counts(good, total)


# ---------------------------------------------------------------- the numbers


def test_no_events_is_unknown_never_100_percent() -> None:
    empty = Counts(0, 0)
    assert compliance(empty) == Unknown("slo.no_events")
    assert burn_rate(empty, T995) == Unknown("slo.no_events")
    assert budget_left(empty, T995) == Unknown("slo.no_events")


def test_numbers_for_a_known_case() -> None:
    # 1,000 checks, 2 failed, target 99.5%: 99.8% good, 40% of the budget spent.
    counts = Counts(998, 1000)
    assert compliance(counts) == Fraction(998, 1000)
    assert burn_rate(counts, T995) == Fraction(2, 5)
    assert budget_left(counts, T995) == Fraction(3, 5)


def test_overspent_budget_goes_below_zero() -> None:
    counts = Counts(980, 1000)  # 2% bad against a 0.5% budget
    assert burn_rate(counts, T995) == 4
    assert budget_left(counts, T995) == -3


def test_all_good_leaves_the_whole_budget() -> None:
    assert budget_left(Counts(10, 10), T995) == 1
    assert burn_rate(Counts(10, 10), T995) == 0


@given(
    total=st.integers(min_value=1, max_value=10**6),
    data=st.data(),
    target=st.fractions(min_value=Fraction(1, 100), max_value=Fraction(9999, 10000)),
)
def test_budget_left_and_burn_rate_always_add_to_one(
    total: int, data: st.DataObject, target: Fraction
) -> None:
    if target >= 1:  # st.fractions may land exactly on the bound
        return
    good = data.draw(st.integers(min_value=0, max_value=total))
    counts = Counts(good, total)
    rate = burn_rate(counts, target)
    left = budget_left(counts, target)
    assert isinstance(rate, Fraction)
    assert isinstance(left, Fraction)
    assert rate + left == 1
    assert rate >= 0
    assert compliance(counts) == Fraction(good, total)


# ---------------------------------------------------------------- burn alerts


def _windows(rates: dict[timedelta, Fraction], total: int = 100_000) -> dict[timedelta, Counts]:
    """Counts whose burn rate against 99.5% is exactly ``rates[window]``."""
    out = {}
    for window, rate in rates.items():
        bad = rate * Fraction(5, 1000) * total
        assert bad.denominator == 1
        out[window] = Counts(total - int(bad), total)
    return out


def test_rules_and_windows_are_the_workbook_ones() -> None:
    assert [(r.long, r.short, r.rate, r.severity) for r in BURN_RULES] == [
        (H1, M5, Fraction(144, 10), Severity.P1),
        (H6, M30, Fraction(6), Severity.P1),
        (D3, H6, Fraction(1), Severity.P2),
    ]
    assert frozenset({H1, M5, H6, M30, D3}) == BURN_WINDOWS


def test_fast_burn_in_both_windows_is_p1() -> None:
    windows = _windows({H1: Fraction(144, 10), M5: Fraction(20)})
    assert burn_alert(T995, windows) is Severity.P1


def test_fast_burn_that_has_stopped_does_not_alert() -> None:
    # The hour still looks bad, but the last 5 minutes are clean: the outage is over.
    windows = _windows({H1: Fraction(20), M5: Fraction(0)})
    assert burn_alert(T995, windows) is None


def test_just_under_the_threshold_does_not_alert() -> None:
    windows = _windows({H1: Fraction(143, 10), M5: Fraction(20)})
    assert burn_alert(T995, windows) is None


def test_six_hour_burn_is_p1() -> None:
    windows = _windows({H6: Fraction(6), M30: Fraction(6)})
    assert burn_alert(T995, windows) is Severity.P1


def test_slow_burn_over_three_days_is_p2() -> None:
    windows = _windows({D3: Fraction(1), H6: Fraction(1)})
    assert burn_alert(T995, windows) is Severity.P2


def test_loudest_rule_wins() -> None:
    windows = _windows({H1: Fraction(15), M5: Fraction(15), D3: Fraction(2), H6: Fraction(2)})
    assert burn_alert(T995, windows) is Severity.P1


def test_missing_or_empty_windows_never_fire() -> None:
    assert burn_alert(T995, {}) is None
    assert burn_alert(T995, {H1: Counts(0, 10), M5: Counts(0, 0)}) is None
    assert burn_alert(T995, {H1: Counts(0, 10)}) is None


def test_all_good_never_alerts() -> None:
    assert burn_alert(T995, {w: Counts(50, 50) for w in BURN_WINDOWS}) is None

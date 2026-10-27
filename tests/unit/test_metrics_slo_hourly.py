"""An objective worked out from hourly counts (the checker's availability counts, ADR-032)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from fractions import Fraction

import pytest

from agentzaaheen.core.metrics import Counts, Objective, SliKind, Unknown, hourly_status
from agentzaaheen.core.sdk import Severity

NOW = datetime(2026, 10, 27, 16, 20, tzinfo=UTC)
TOP = NOW.replace(minute=0)  # the current hour's start
AVAILABILITY = Objective(
    project="eva",
    name="eva-availability",
    kind=SliKind.AVAILABILITY,
    target=Fraction(995, 1000),
    window=timedelta(days=28),
)
SPEED = Objective(
    project="eva",
    name="eva-api-speed",
    kind=SliKind.LATENCY,
    target=Fraction(95, 100),
    window=timedelta(days=28),
    threshold_ms=800,
)


def hours_back(n: int, counts: Counts) -> list[tuple[datetime, Counts]]:
    """``n`` hours of ``counts``, ending with the current hour."""
    return [(TOP - timedelta(hours=i), counts) for i in range(n)]


def test_only_hours_that_started_inside_the_window_count() -> None:
    inside = hours_back(28 * 24, Counts(60, 60))
    outside = [(TOP - timedelta(days=29), Counts(0, 60))]
    future = [(TOP + timedelta(hours=1), Counts(0, 60))]  # a clock ahead of ours never counts
    status = hourly_status(AVAILABILITY, inside + outside + future, NOW)
    assert status.counts == Counts(28 * 24 * 60, 28 * 24 * 60)
    assert status.compliance == 1
    # Counted by whole hours: a full window is covered to within the current hour.
    assert AVAILABILITY.window - timedelta(hours=1) < status.measured <= AVAILABILITY.window


def test_a_young_history_says_how_much_of_the_window_it_covers() -> None:
    status = hourly_status(AVAILABILITY, hours_back(48, Counts(59, 60)), NOW)
    assert status.measured == NOW - (TOP - timedelta(hours=47))
    assert status.compliance == Fraction(59, 60)
    # 1 in 60 failed against an allowance of 1 in 200: 3 1/3 times over, so the budget is overspent.
    assert status.budget_left == 1 - Fraction(1, 60) / Fraction(5, 1000)
    assert status.time_left == timedelta(0)  # overspent shows as nothing left, never as negative time


def test_no_counts_is_unknown_never_fine() -> None:
    status = hourly_status(AVAILABILITY, [], NOW)
    assert isinstance(status.compliance, Unknown)
    assert isinstance(status.budget_left, Unknown)
    assert (status.measured, status.burn, status.time_left) == (timedelta(0), None, None)


def test_the_budget_left_is_also_given_as_time_for_availability() -> None:
    status = hourly_status(AVAILABILITY, hours_back(100, Counts(399, 400)), NOW)
    assert status.budget_left == Fraction(1, 2)  # 1 in 400 failed against 1 in 200
    assert status.time_left == AVAILABILITY.allowed_downtime / 2


def test_a_speed_objective_has_no_time_left() -> None:
    assert hourly_status(SPEED, hours_back(10, Counts(99, 100)), NOW).time_left is None


def test_three_bad_days_still_bad_in_the_last_six_hours_is_a_slow_burn() -> None:
    status = hourly_status(AVAILABILITY, hours_back(72, Counts(0, 60)), NOW)
    assert status.burn is Severity.P2


def test_a_slow_burn_that_has_stopped_does_not_alert() -> None:
    recovered = hours_back(6, Counts(60, 60))
    before = [(TOP - timedelta(hours=6 + i), Counts(0, 60)) for i in range(66)]
    assert hourly_status(AVAILABILITY, recovered + before, NOW).burn is None


@pytest.mark.parametrize("hours", [1, 6])
def test_a_fresh_outage_is_at_most_a_slow_burn_from_hourly_counts(hours: int) -> None:
    # The fast rules (5- and 30-minute windows) cannot be judged from hourly counts, so P1 never
    # comes from here: the checker puts a down project on the phone within 60 seconds instead.
    before = [(TOP - timedelta(hours=hours + i), Counts(60, 60)) for i in range(72)]
    status = hourly_status(AVAILABILITY, hours_back(hours, Counts(0, 60)) + before, NOW)
    assert status.burn is Severity.P2

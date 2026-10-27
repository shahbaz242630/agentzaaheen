"""A project's "Uptime target" card: numbers from code, rounded down, unknown when not fresh (F07 step 5b)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from fractions import Fraction

import pytest

from agentzaaheen.core.api.slo import CARD_ID, NOT_YET, slo_card
from agentzaaheen.core.api.views import Card, NotKnown, Percent, Tone
from agentzaaheen.core.metrics import Counts, Freshness, Objective, SliKind, hourly_status

NOW = datetime(2026, 10, 27, 16, 20, tzinfo=UTC)
TOP = NOW.replace(minute=0)
HOUR = timedelta(hours=1)
TARGET = Objective(
    project="eva",
    name="eva-availability",
    kind=SliKind.AVAILABILITY,
    target=Fraction(995, 1000),
    window=timedelta(days=28),
)


def card(good: int, total: int, hours: int) -> Card:
    counts = [(TOP - timedelta(hours=i), Counts(good, total)) for i in range(hours)]
    return slo_card(TARGET, hourly_status(TARGET, counts, NOW), read_at=NOW, schedule=HOUR, now=NOW)


def test_within_target_for_the_whole_window() -> None:
    shown = card(400, 400, 28 * 24 + 1)
    assert (shown.id, shown.sample, shown.freshness) == (CARD_ID, False, Freshness.FRESH)
    up, left = shown.metrics
    assert up.value == Percent(basis_points=10_000)
    assert up.detail == "of 268,800 checks; target 99.50%"
    assert left.value == Percent(basis_points=10_000)
    assert left.detail == "about 3 h 21 min of 3 h 21 min"
    assert [(e.text, e.tone) for e in shown.entries] == [
        ("Within target over 28 days", Tone.OK),
        (NOT_YET, Tone.INFO),
    ]


def test_a_young_window_says_how_much_was_measured() -> None:
    assert card(400, 400, 48).entries[0].text == "Within target over the 2.0 days measured so far"


def test_shares_are_rounded_down_so_never_shown_better() -> None:
    up = card(39_998, 40_000, 10).metrics[0].value  # 99.995%
    assert up == Percent(basis_points=9_999)


def test_under_a_quarter_of_the_budget_left_is_a_warning() -> None:
    shown = card(1_992, 2_000, 10)  # 0.4% failed of 0.5% allowed: 80% spent, 20% left
    verdict = shown.entries[0]
    assert (
        verdict.text
        == "Within target over the 0.4 days measured so far, but under a quarter of the budget is left"
    )
    assert verdict.tone is Tone.WARN
    assert shown.metrics[1].value == Percent(basis_points=2_000)


def test_spent_says_how_far_over_and_shows_no_negative_share() -> None:
    shown = card(0, 60, 72)
    left = shown.metrics[1]
    assert left.value == Percent(basis_points=0)
    assert left.detail == "spent: failures ran at 200.0x the target's allowance (3 h 21 min)"
    texts = [(e.text, e.tone) for e in shown.entries]
    assert texts[0] == (
        "Below target over the 3.0 days measured so far: the error budget is spent",
        Tone.ALERT,
    )
    assert ("Spending the budget too fast over the last 3 days (slow burn)", Tone.WARN) in texts


@pytest.mark.parametrize(
    ("status_read", "read_at", "reason", "fresh"),
    [
        (False, None, "freshness.never", Freshness.NEVER),
        (True, NOW - 3 * HOUR, "freshness.stale", Freshness.STALE),
    ],
)
def test_never_read_or_too_old_is_unknown_never_fine(
    status_read: bool, read_at: datetime | None, reason: str, fresh: Freshness
) -> None:
    status = hourly_status(TARGET, [(TOP, Counts(60, 60))], NOW) if status_read else None
    shown = slo_card(TARGET, status, read_at=read_at, schedule=HOUR, now=NOW)
    assert shown.freshness is fresh
    assert {m.value for m in shown.metrics} == {NotKnown(reason=reason)}
    assert [e.text for e in shown.entries] == [NOT_YET]


def test_no_checks_counted_is_unknown_and_said() -> None:
    shown = slo_card(TARGET, hourly_status(TARGET, [], NOW), read_at=NOW, schedule=HOUR, now=NOW)
    assert {m.value for m in shown.metrics} == {NotKnown(reason="slo.no_events")}
    assert shown.entries[0].text == "No checks were counted in the window yet"


def test_exactly_at_target_means_the_budget_is_spent() -> None:
    shown = card(995, 1_000, 10)  # 99.5% up: every allowed failure used
    assert shown.entries[0].tone is Tone.ALERT
    assert shown.entries[0].text.endswith("the error budget is spent")

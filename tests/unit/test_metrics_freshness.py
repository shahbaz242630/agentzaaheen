"""The freshness rule (PRD F1.7): fresh, stale (> 2x schedule), alert (> 6x), never synced."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from yaqzan.core.metrics import Freshness, freshness

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
HOUR = timedelta(hours=1)


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(0), Freshness.FRESH),
        (2 * HOUR, Freshness.FRESH),  # exactly 2x is still fresh
        (2 * HOUR + timedelta(seconds=1), Freshness.STALE),
        (6 * HOUR, Freshness.STALE),
        (6 * HOUR + timedelta(seconds=1), Freshness.ALERT),
        (-HOUR, Freshness.FRESH),  # a success "in the future" (clock skew) is not stale
    ],
)
def test_freshness_bands(age: timedelta, expected: Freshness) -> None:
    assert freshness(NOW - age, HOUR, NOW) is expected


def test_never_synced_is_never_and_unusable() -> None:
    assert freshness(None, HOUR, NOW) is Freshness.NEVER
    assert not Freshness.NEVER.usable
    assert not Freshness.STALE.usable
    assert not Freshness.ALERT.usable
    assert Freshness.FRESH.usable


@pytest.mark.parametrize("schedule", [timedelta(0), -HOUR])
def test_schedule_must_be_positive(schedule: timedelta) -> None:
    with pytest.raises(ValueError, match="positive"):
        freshness(NOW, schedule, NOW)


@pytest.mark.parametrize(("last", "now"), [(datetime(2026, 10, 18), NOW), (NOW, datetime(2026, 10, 18))])  # noqa: DTZ001
def test_naive_times_are_refused(last: datetime, now: datetime) -> None:
    with pytest.raises(ValueError, match="timezone"):
        freshness(last, HOUR, now)

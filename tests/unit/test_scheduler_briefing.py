"""When the morning summaries are made and the briefing is sent (F17 slice 6).

Dubai has no clock changes, so ``Europe/London`` is tested too (for later users):
the times stay 05:00 and 07:00 on the wall clock across both changes, and the
day being summarised is 23 or 25 hours long.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agentzaaheen.core.scheduler import local_day, plan, window_start

DUBAI = ZoneInfo("Asia/Dubai")
LONDON = ZoneInfo("Europe/London")


def utc(hour: int, minute: int = 0, day: int = 25, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


@pytest.mark.parametrize(
    ("now", "gather", "send"),
    [
        (utc(0, 59), False, False),  # 04:59 in Dubai
        (utc(1, 0), True, False),  # 05:00: gather
        (utc(2, 59), True, False),  # 06:59: still before the briefing
        (utc(3, 0), True, True),  # 07:00: both (a dashboard started now catches up)
    ],
)
def test_gather_at_five_and_brief_at_seven_dubai_time(now: datetime, gather: bool, send: bool) -> None:
    due = plan(now, DUBAI, gathered=False, briefed="none")
    assert (due.gather, due.send, due.day) == (gather, send, date(2026, 10, 25))


def test_done_today_means_nothing_is_due() -> None:
    due = plan(utc(5), DUBAI, gathered=True, briefed="sent")
    assert (due.gather, due.send) == (False, False)


@pytest.mark.parametrize("briefed", ["waiting", "not_sent"])
def test_a_briefing_saved_but_not_sent_is_tried_again(briefed: str) -> None:
    due = plan(utc(4), DUBAI, gathered=True, briefed=briefed)  # type: ignore[arg-type]
    assert due.send
    assert not due.late  # its words were already made; only a fresh one says it is late


def test_a_briefing_made_after_half_past_seven_says_it_is_late() -> None:
    assert not plan(utc(3, 30), DUBAI, gathered=True, briefed="none").late
    late = plan(utc(6, 2), DUBAI, gathered=True, briefed="none")
    assert (late.late, late.local_time) == (True, "10:02")


def test_just_after_midnight_dubai_is_the_new_day_with_nothing_due_yet() -> None:
    due = plan(utc(20, 30, day=25), DUBAI, gathered=False, briefed="none")  # 00:30 on the 26th
    assert (due.day, due.gather, due.send) == (date(2026, 10, 26), False, False)


def test_the_window_is_the_day_since_five_the_morning_before() -> None:
    assert window_start(date(2026, 10, 25), DUBAI) == utc(1, day=24)


def test_london_keeps_wall_clock_times_across_the_october_change() -> None:
    # 25 Oct 2026: clocks go back at 02:00 BST. 05:00 local is then 05:00 UTC (GMT).
    assert plan(utc(4, 59), LONDON, gathered=False, briefed="none").gather is False
    assert plan(utc(5, 0), LONDON, gathered=False, briefed="none").gather is True
    start = window_start(date(2026, 10, 25), LONDON)
    assert start == utc(4, day=24)  # 05:00 BST the day before
    assert utc(5) - start == timedelta(hours=25)


def test_london_keeps_wall_clock_times_across_the_march_change() -> None:
    # 29 Mar 2026: clocks go forward at 01:00 GMT. 05:00 local is then 04:00 UTC (BST).
    assert plan(utc(3, 59, day=29, month=3), LONDON, gathered=False, briefed="none").gather is False
    assert plan(utc(4, 0, day=29, month=3), LONDON, gathered=False, briefed="none").gather is True
    start = window_start(date(2026, 3, 29), LONDON)
    assert utc(4, day=29, month=3) - start == timedelta(hours=23)


def test_a_time_without_a_zone_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        plan(datetime(2026, 10, 25, 7, 0), DUBAI, gathered=False, briefed="none")  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone-aware"):
        local_day(datetime(2026, 10, 25, 7, 0), DUBAI)  # noqa: DTZ001

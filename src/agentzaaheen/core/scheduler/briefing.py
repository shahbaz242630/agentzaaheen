"""When the morning summaries are made and the briefing is sent (F17 slice 6, ADR-039).

Pure clock rules, no database or network: :func:`plan` is given the time now,
the Chairman's time zone, and what has been done today, and says what is due.

- Summaries are **gathered at 05:00** local time, covering the day since 05:00
  the day before (23 or 25 hours across a clock change, never a wrong day).
- The briefing goes at **07:00** local time, after the summaries.
- Catch-up, once: a dashboard started later the same day still gathers and
  sends, and the briefing says it is late. A day that has passed is not sent
  afterwards (the next morning's briefing covers it).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

GATHER_AT = time(5, 0)
BRIEF_AT = time(7, 0)
LATE_AFTER = timedelta(minutes=30)  # sent later than 07:30 says it is late
DEFAULT_ZONE = "Asia/Dubai"  # the Chairman is in the UAE

type Briefed = Literal["none", "waiting", "sent", "not_sent"]


@dataclass(frozen=True)
class Plan:
    """What is due now. ``window_start`` is when the day being summarised began (UTC)."""

    day: date
    gather: bool
    send: bool
    late: bool
    local_time: str
    window_start: datetime


def local_day(now: datetime, zone: ZoneInfo) -> date:
    """The Chairman's date at ``now``."""
    _aware(now)
    return now.astimezone(zone).date()


def window_start(day: date, zone: ZoneInfo) -> datetime:
    """When the day summarised on ``day`` began: 05:00 local the day before, in UTC."""
    return datetime.combine(day - timedelta(days=1), GATHER_AT, tzinfo=zone).astimezone(UTC)


def plan(now: datetime, zone: ZoneInfo, *, gathered: bool, briefed: Briefed) -> Plan:
    """What is due at ``now``: gathering today's summaries, sending today's briefing, or nothing."""
    _aware(now)
    local = now.astimezone(zone)
    day = local.date()
    brief_time = datetime.combine(day, BRIEF_AT, tzinfo=zone)
    gather = local.time() >= GATHER_AT and not gathered
    send = local >= brief_time and briefed in {"none", "waiting", "not_sent"}
    return Plan(
        day=day,
        gather=gather,
        send=send,
        late=send and briefed == "none" and local > brief_time + LATE_AFTER,
        local_time=local.strftime("%H:%M"),
        window_start=window_start(day, zone),
    )


def _aware(now: datetime) -> None:
    if now.tzinfo is None:
        raise ValueError("the time must be timezone-aware")

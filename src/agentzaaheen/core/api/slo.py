"""A project's uptime target as a dashboard card (ADR-032, F07 step 5b).

The card shows the availability objective worked out by ``core.metrics``: the
share of external checks that found the project up over the window, and how
much of the error budget is left. Its lines say in plain words whether the
project is within target, whether the budget is spent or burning, and that
speed and error targets are not connected yet. Like every real card, its
freshness is judged when it is shown: counts older than the rule allows (or
never read) turn every number "unknown". Percentages are rounded down, so the
card never shows a project as better than it was.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from fractions import Fraction

from agentzaaheen.core import metrics
from agentzaaheen.core.api.views import Card, Entry, Metric, NotKnown, Percent, Tone
from agentzaaheen.core.metrics import Freshness, Objective, SloStatus
from agentzaaheen.core.metrics.revenue import Unknown

CARD_ID = "slo"
TITLE = "Uptime target"
SOURCE = "External checker (live)"
LABELS = ("Up (28 days)", "Error budget left")
LOW = Fraction(1, 4)  # under a quarter of the budget left: be careful with changes
NOT_YET = "Speed and error targets are not connected yet: the product does not send them"


def slo_card(
    objective: Objective,
    status: SloStatus | None,
    *,
    read_at: datetime | None,
    schedule: timedelta,
    now: datetime,
) -> Card:
    """One project's uptime-target card; ``status`` is ``None`` before the first read."""
    fresh = metrics.freshness(read_at if status is not None else None, schedule, now)
    if status is None or not fresh.usable:
        return _unknown(fresh, NotKnown(reason=f"freshness.{fresh.value}"), (), read_at if status else None)
    up, left = status.compliance, status.budget_left
    if isinstance(up, Unknown) or isinstance(left, Unknown):
        line = Entry(text="No checks were counted in the window yet", tone=Tone.WARN, at=read_at)
        return _unknown(fresh, NotKnown(reason="slo.no_events"), (line,), read_at)
    numbers = (
        Metric(
            label=LABELS[0],
            value=Percent(basis_points=_floor_bp(up)),
            detail=f"of {status.counts.total:,} checks; target {_bp_text(_floor_bp(objective.target))}",
        ),
        Metric(
            label=LABELS[1],
            value=Percent(basis_points=_floor_bp(max(left, Fraction(0)))),
            detail=_budget_detail(status, left),
        ),
    )
    lines = [_verdict(status, up >= objective.target, left)]
    if status.burn is not None:
        lines.append(
            Entry(text="Spending the budget too fast over the last 3 days (slow burn)", tone=Tone.WARN)
        )
    lines.append(Entry(text=NOT_YET, tone=Tone.INFO))
    return _card(fresh, numbers, tuple(lines), read_at)


def _verdict(status: SloStatus, within: bool, left: Fraction) -> Entry:
    objective = status.objective
    if objective.window - status.measured < timedelta(hours=1):  # counted by whole hours
        span = f"over {objective.window.days} days"
    else:
        span = f"over the {status.measured.total_seconds() / 86400:.1f} days measured so far"
    if not within or left <= 0:
        return Entry(text=f"Below target {span}: the error budget is spent", tone=Tone.ALERT)
    if left < LOW:
        return Entry(text=f"Within target {span}, but under a quarter of the budget is left", tone=Tone.WARN)
    return Entry(text=f"Within target {span}", tone=Tone.OK)


def _budget_detail(status: SloStatus, left: Fraction) -> str:
    allowed = _duration(status.objective.allowed_downtime)
    if left <= 0:
        return f"spent: failures ran at {float(1 - left):.1f}x the target's allowance ({allowed})"
    time_left = status.time_left
    return f"about {_duration(time_left)} of {allowed}" if time_left is not None else f"of {allowed}"


def _floor_bp(share: Fraction) -> int:
    return min(10_000, max(0, math.floor(share * 10_000)))


def _bp_text(basis_points: int) -> str:
    whole, rest = divmod(basis_points, 100)
    return f"{whole}.{rest:02d}%".replace(".00%", "%")


def _duration(span: timedelta) -> str:
    minutes = int(span.total_seconds()) // 60
    return f"{minutes // 60} h {minutes % 60} min"


def _unknown(fresh: Freshness, why: NotKnown, lines: tuple[Entry, ...], read_at: datetime | None) -> Card:
    numbers = tuple(Metric(label=label, value=why) for label in LABELS)
    return _card(fresh, numbers, (*lines, Entry(text=NOT_YET, tone=Tone.INFO)), read_at)


def _card(
    fresh: Freshness, numbers: tuple[Metric, ...], lines: tuple[Entry, ...], read_at: datetime | None
) -> Card:
    return Card(
        id=CARD_ID,
        title=TITLE,
        source=SOURCE,
        freshness=fresh,
        last_success_at=read_at,
        sample=False,
        metrics=numbers,
        entries=lines,
    )

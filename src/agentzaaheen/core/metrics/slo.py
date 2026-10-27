"""Service level objectives: what "good" looks like, decided before anything breaks (ADR-032).

An objective says what share of events must be good over a rolling window,
for example 99.5% of uptime checks over 28 days. From counts of good and total
events this module works out compliance, how much of the error budget is left
and how fast it is burning. Burn alerts follow the multi-window rule of the
Google SRE workbook (chapter 5): a long window shows the burn is real, a short
one shows it is still happening, so an outage that has ended stops alerting.

No events means "unknown", never 100% (PRD F1.7). Exact fractions throughout.

:func:`hourly_status` works an objective out from counts kept by the hour
(the checker's availability counts). Windows are then whole hours: an hour
counts if it started inside the window. So only burn rules whose two
windows are both at least an hour can be judged (the 3-day + 6-hour slow
burn); the fast ones (5 and 30 minutes) never fire from hourly counts. A
fast outage is already on the phone within 60 seconds from the checker.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from fractions import Fraction

from agentzaaheen.core.metrics.revenue import Unknown
from agentzaaheen.core.sdk import Severity

MIN_WINDOW = timedelta(days=1)
MAX_WINDOW = timedelta(days=90)
NO_EVENTS = Unknown("slo.no_events")
HOUR = timedelta(hours=1)


class SliKind(StrEnum):
    """What an objective counts as a good event."""

    AVAILABILITY = "availability"  # an external check that found the service up
    LATENCY = "latency"  # a request answered within the threshold
    ERRORS = "errors"  # a request answered without a server error


@dataclass(frozen=True)
class Objective:
    """One SLO: ``target`` share of ``kind`` events good over a rolling ``window``."""

    project: str
    name: str
    kind: SliKind
    target: Fraction
    window: timedelta
    threshold_ms: int | None = None

    def __post_init__(self) -> None:
        if not 0 < self.target < 1:
            raise ValueError("a target must be between 0% and 100%, both excluded")
        if not MIN_WINDOW <= self.window <= MAX_WINDOW:
            raise ValueError("a window must be between 1 and 90 days")
        if (self.kind is SliKind.LATENCY) != (self.threshold_ms is not None):
            raise ValueError("a latency objective needs a threshold, and only a latency objective has one")
        if self.threshold_ms is not None and self.threshold_ms <= 0:
            raise ValueError("a latency threshold must be a positive number of milliseconds")

    @property
    def budget(self) -> Fraction:
        """The share of events allowed to be bad (100% minus the target)."""
        return 1 - self.target

    @property
    def allowed_downtime(self) -> timedelta:
        """The error budget as time, for an availability objective measured evenly over its window."""
        return self.window * self.budget.numerator / self.budget.denominator


@dataclass(frozen=True)
class Counts:
    """Events in one window: how many there were and how many were good."""

    good: int
    total: int

    def __post_init__(self) -> None:
        if not 0 <= self.good <= self.total:
            raise ValueError("good events must be between 0 and the total")


def compliance(counts: Counts) -> Fraction | Unknown:
    """The share of events that were good."""
    if counts.total == 0:
        return NO_EVENTS
    return Fraction(counts.good, counts.total)


def burn_rate(counts: Counts, target: Fraction) -> Fraction | Unknown:
    """How fast the budget is being spent: 1 spends it exactly over the window, 10 in a tenth of it."""
    if counts.total == 0:
        return NO_EVENTS
    return Fraction(counts.total - counts.good, counts.total) / (1 - target)


def budget_left(counts: Counts, target: Fraction) -> Fraction | Unknown:
    """The share of the window's error budget still unspent: 1 untouched, 0 spent, below 0 overspent."""
    rate = burn_rate(counts, target)
    if isinstance(rate, Unknown):
        return rate
    return 1 - rate


@dataclass(frozen=True)
class BurnRule:
    """Alert when both windows burn at least ``rate`` times the sustainable pace."""

    long: timedelta
    short: timedelta
    rate: Fraction
    severity: Severity


# Google SRE workbook, table 5-8: 2% of a month's budget in an hour, 5% in six
# hours (both page, P1); 10% in three days (a ticket, P2).
BURN_RULES = (
    BurnRule(timedelta(hours=1), timedelta(minutes=5), Fraction(144, 10), Severity.P1),
    BurnRule(timedelta(hours=6), timedelta(minutes=30), Fraction(6), Severity.P1),
    BurnRule(timedelta(days=3), timedelta(hours=6), Fraction(1), Severity.P2),
)
BURN_WINDOWS = frozenset(w for rule in BURN_RULES for w in (rule.long, rule.short))


def burn_alert(target: Fraction, windows: Mapping[timedelta, Counts]) -> Severity | None:
    """The loudest burn rule that fires, or ``None``. A window with no events never fires."""
    fired: list[Severity] = []
    for rule in BURN_RULES:
        rates = [burn_rate(windows.get(w, Counts(0, 0)), target) for w in (rule.long, rule.short)]
        if all(not isinstance(r, Unknown) and r >= rule.rate for r in rates):
            fired.append(rule.severity)
    return min(fired) if fired else None  # "P1" sorts before "P2"


@dataclass(frozen=True)
class SloStatus:
    """How one objective stands now: its window's counts and what code works out from them."""

    objective: Objective
    counts: Counts
    measured: timedelta  # how much of the window has counts so far (at most the window)
    compliance: Fraction | Unknown
    budget_left: Fraction | Unknown
    burn: Severity | None

    @property
    def time_left(self) -> timedelta | None:
        """For availability: the budget still unspent as time over the whole window (``None``: unknown)."""
        if self.objective.kind is not SliKind.AVAILABILITY or isinstance(self.budget_left, Unknown):
            return None
        left = max(self.budget_left, Fraction(0))
        return self.objective.allowed_downtime * left.numerator / left.denominator


def hourly_status(objective: Objective, hours: Iterable[tuple[datetime, Counts]], now: datetime) -> SloStatus:
    """``objective`` from counts kept per hour (each hour's start and its counts), as of ``now``."""
    kept = [(start, counts) for start, counts in hours if start <= now]
    in_window = [(start, counts) for start, counts in kept if start >= now - objective.window]
    counts = _sum(c for _, c in in_window)
    first = min((start for start, c in in_window if c.total), default=None)
    measured = timedelta(0) if first is None else min(objective.window, now - first)
    windows = {w: _sum(c for start, c in kept if start >= now - w) for w in BURN_WINDOWS if w >= HOUR}
    return SloStatus(
        objective=objective,
        counts=counts,
        measured=measured,
        compliance=compliance(counts),
        budget_left=budget_left(counts, objective.target),
        burn=burn_alert(objective.target, windows),
    )


def _sum(counts: Iterable[Counts]) -> Counts:
    good = total = 0
    for each in counts:
        good += each.good
        total += each.total
    return Counts(good, total)

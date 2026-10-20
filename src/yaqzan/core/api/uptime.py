"""Turn a project's uptime check into its dashboard card (F03 slice 4).

The card counts the addresses that are up and down and gives the slowest
answer; each address that is down (or slow) gets a line saying why in plain
words. Like the Code card, its freshness is judged when it is shown: once the
check is older than its rule allows, every number turns "unknown" and the
lines are dropped, so an old "all up" can never look current.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from yaqzan.core import metrics
from yaqzan.core.api.views import Card, Count, Entry, Metric, NotKnown, Tone
from yaqzan.core.sdk import SiteCheck, UptimeReport

CARD_ID = "uptime"
TITLE = "Uptime"
LABELS = ("Up", "Down", "Slowest answer (ms)")
SLOW_MS = 3000
_MAX_ENTRY = 160
_WHY = {
    "uptime.no_answer": "did not answer",
    "uptime.server_error": "server error",
    "uptime.not_found": "page not found",
    "uptime.refused": "refused the request",
}


def uptime_card(report: UptimeReport, *, schedule: timedelta, now: datetime) -> Card:
    """One project's uptime card, fresh or (once too old) all unknown."""
    fresh = metrics.freshness(report.fetched_at, schedule, now)
    seen = report.fetched_at
    if not fresh.usable:
        gone = NotKnown(reason=f"freshness.{fresh.value}")
        unknown = tuple(Metric(label=label, value=gone) for label in LABELS)
        return _card(fresh, unknown, (), last_success_at=seen)
    total = len(report.sites)
    timed = [site.latency_ms for site in report.sites if site.latency_ms is not None]
    slowest: Count | NotKnown = Count(value=max(timed)) if timed else NotKnown(reason="uptime.no_answer")
    numbers = (
        Metric(label=LABELS[0], value=Count(value=report.up), detail=f"of {total} addresses"),
        Metric(label=LABELS[1], value=Count(value=total - report.up)),
        Metric(label=LABELS[2], value=slowest),
    )
    return _card(fresh, numbers, _lines(report), last_success_at=seen)


def _lines(report: UptimeReport) -> tuple[Entry, ...]:
    down = [_down(site) for site in report.sites if not site.up]
    slow = [
        Entry(text=_fit(f"{_where(site)} is slow: {site.latency_ms} ms"), tone=Tone.WARN, at=site.checked_at)
        for site in report.sites
        if site.up and site.latency_ms is not None and site.latency_ms > SLOW_MS
    ]
    if down or slow:
        return (*down, *slow)
    total = len(report.sites)
    return (Entry(text=f"All {total} addresses answered as they should", tone=Tone.OK, at=report.fetched_at),)


def _down(site: SiteCheck) -> Entry:
    why = _WHY.get(site.reason, "not as expected")
    status = f" (http {site.status})" if site.status is not None else ""
    return Entry(text=_fit(f"{_where(site)} is down: {why}{status}"), tone=Tone.ALERT, at=site.checked_at)


def _where(site: SiteCheck) -> str:
    return site.url.removeprefix("https://").rstrip("/")


def _card(
    fresh: metrics.Freshness,
    numbers: tuple[Metric, ...],
    lines: tuple[Entry, ...],
    *,
    last_success_at: datetime,
) -> Card:
    return Card(
        id=CARD_ID,
        title=TITLE,
        source="Uptime (live)",
        freshness=fresh,
        last_success_at=last_success_at,
        sample=False,
        metrics=numbers,
        entries=lines,
    )


def _fit(text: str) -> str:
    return text if len(text) <= _MAX_ENTRY else text[: _MAX_ENTRY - 1].rstrip() + "…"

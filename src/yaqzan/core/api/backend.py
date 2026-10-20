"""Turn a backend project's health into its dashboard card (F03 slice 6).

The card counts healthy services, gives the disk in use and the last day's
API requests; anything wrong gets a line in plain words. Like the other real
cards, its freshness is judged when it is shown, and a project that could not
be read says why instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta

from yaqzan.core import metrics
from yaqzan.core.api.views import Card, Count, Entry, Metric, NotKnown, Percent, Tone
from yaqzan.core.sdk import BackendHealth, ProjectState, explain

CARD_ID = "supabase"
TITLE = "Backend"
LABELS = ("Services healthy", "Disk used", "API requests (24h)")
DISK_WARN_BP = 8_000  # 80%
DISK_ALERT_BP = 9_000  # 90%
_GB = 1_000_000_000
_MAX_ENTRY = 160
_STATE_WORDS: Mapping[ProjectState, str] = {
    ProjectState.UNHEALTHY: "is running but unhealthy",
    ProjectState.STARTING: "is starting up or being changed",
    ProjectState.PAUSED: "is paused",
    ProjectState.FAILED: "failed to start, restore or pause",
    ProjectState.REMOVED: "has been removed",
    ProjectState.UNKNOWN: "is in a state Yaqzan does not know",
}


def backend_card(health: BackendHealth, *, source_label: str, schedule: timedelta, now: datetime) -> Card:
    """One backend project's card, fresh or (once too old) all unknown."""
    fresh = metrics.freshness(health.fetched_at, schedule, now)
    seen = health.fetched_at
    if not fresh.usable:
        gone = NotKnown(reason=f"freshness.{fresh.value}")
        unknown = tuple(Metric(label=label, value=gone) for label in LABELS)
        return _card(fresh, unknown, (), source_label=source_label, last_success_at=seen)
    disk_bp = health.disk_basis_points
    size_gb = health.disk_size_bytes / _GB if health.disk_size_bytes else None
    numbers = (
        Metric(
            label=LABELS[0],
            value=Count(value=health.healthy_services),
            detail=f"of {len(health.services)} services",
        ),
        Metric(
            label=LABELS[1],
            value=Percent(basis_points=disk_bp)
            if disk_bp is not None
            else NotKnown(reason="source.unavailable"),
            detail=f"of {size_gb:.1f} GB" if size_gb is not None else None,
        ),
        Metric(
            label=LABELS[2],
            value=Count(value=health.requests_24h)
            if health.requests_24h is not None
            else NotKnown(reason="source.unavailable"),
        ),
    )
    return _card(fresh, numbers, _lines(health), source_label=source_label, last_success_at=seen)


def failed_backend_card(code: str, *, source_label: str) -> Card:
    """The card for a project that could not be read: every number unknown, and why in words."""
    said = explain(code, vendor="Supabase")
    numbers = tuple(Metric(label=label, value=NotKnown(reason=code)) for label in LABELS)
    line = Entry(text=_fit(f"{said.what} {said.fix}"), tone=Tone.WARN)
    return _card(metrics.Freshness.NEVER, numbers, (line,), source_label=source_label, last_success_at=None)


def _lines(health: BackendHealth) -> tuple[Entry, ...]:
    lines: list[Entry] = []
    if health.state is not ProjectState.HEALTHY:
        lines.append(Entry(text=f"The Supabase project {_STATE_WORDS[health.state]}", tone=Tone.ALERT))
    lines += [
        Entry(text=f"The {service.name} service is unhealthy", tone=Tone.ALERT)
        for service in health.services
        if not service.healthy
    ]
    disk_bp = health.disk_basis_points
    if disk_bp is not None and disk_bp >= DISK_WARN_BP:
        tone = Tone.ALERT if disk_bp >= DISK_ALERT_BP else Tone.WARN
        lines.append(Entry(text=f"The database disk is {disk_bp / 100:.0f}% full", tone=tone))
    if not lines:
        lines.append(
            Entry(text=f"All {len(health.services)} services healthy", tone=Tone.OK, at=health.fetched_at)
        )
    return tuple(lines)


def _card(
    fresh: metrics.Freshness,
    numbers: tuple[Metric, ...],
    lines: tuple[Entry, ...],
    *,
    source_label: str,
    last_success_at: datetime | None,
) -> Card:
    return Card(
        id=CARD_ID,
        title=TITLE,
        source=source_label,
        freshness=fresh,
        last_success_at=last_success_at,
        sample=False,
        metrics=numbers,
        entries=lines,
    )


def _fit(text: str) -> str:
    return text if len(text) <= _MAX_ENTRY else text[: _MAX_ENTRY - 1].rstrip() + "…"

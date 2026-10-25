"""A workspace's daily summary and the Board's morning briefing, written by code (F17 slice 6, ADR-039).

Every fact comes from what Yaqzan already holds (PRD F2: every number is put
in by code; no model writes here). :func:`summarise` turns one workspace's
cards, its alerts, agents and the scout's newest advice into a
:class:`DailySummary`. Rules:

- What could not be read is listed **first** (PRD F1.7), with its reason code.
- Only fresh, real cards give numbers; a stale or failed card gives none.
  Sample cards are left out by name, never counted as real.
- No data means "nothing to report", never 0.
- Changes are found by comparing with the newest earlier summary, number by
  number, only where both are known.

:func:`first_line` and :func:`briefing` word the result from fixed templates:
counts, our own words, card titles and workspace names. No text a website, a
build or a repository wrote reaches the phone (ADR-031), and the message has
no links.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from yaqzan.core.api.views import Card, Entry, Health, Metric, NotKnown, ProjectDetail, Tone, Value

SUMMARY_VERSION: Literal[1] = 1
MAX_SOURCES = 12
MAX_METRICS = 6
MAX_LINES = 5
MAX_CHANGES = 8
MAX_ROLES = 40
MAX_MESSAGE_BYTES = 3500  # under the push client's limit and the saved briefing's
_SLUG = r"^[a-z0-9][a-z0-9-]{0,63}$"
_CODE = r"^[a-z0-9_]+(\.[a-z0-9_]+)*$"
_FLAGGED = frozenset({Tone.WARN, Tone.ALERT})
_HEALTH_WORDS = {
    Health.OK: "All fine",
    Health.WARN: "Needs a look",
    Health.ALERT: "Needs you now",
    Health.UNKNOWN: "Health unknown",
}


class _View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Unread(_View):
    """Something that could not be read for the summary, and why (a reason code)."""

    title: str = Field(min_length=1, max_length=40)
    reason: str = Field(pattern=_CODE, max_length=80)


class SourceFact(_View):
    """One fresh, real card as it stood: its numbers, and only the lines that need a look."""

    card: str = Field(pattern=_SLUG)
    title: str = Field(min_length=1, max_length=40)
    metrics: tuple[Metric, ...] = Field(max_length=MAX_METRICS)
    flagged: tuple[Entry, ...] = Field(max_length=MAX_LINES)


class Change(_View):
    """A number that moved since the earlier summary (both values known)."""

    title: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=40)
    before: Value
    after: Value


class AlertsFact(_View):
    """Phone alerts in the window: how many went down and came back up.

    ``counted_since`` is when counting started (the dashboard's start): when it
    is after the window began, the counts cover only part of the day.
    """

    opened: int = Field(ge=0)
    closed: int = Field(ge=0)
    counted_since: AwareDatetime


class AgentsFact(_View):
    """The workspace's agents now: how many, how many on, and spend this month against its cap."""

    agents: int = Field(ge=0)
    on: int = Field(ge=0)
    workspace_on: bool
    spent_minor: int = Field(ge=0)
    cap_minor: int | None = Field(default=None, ge=0)


class ScoutFact(_View):
    """The scout's newest run: how it stands and, when done, which roles it recommends."""

    state: Literal["running", "done", "failed"]
    at: AwareDatetime
    recommended: tuple[str, ...] = Field(default=(), max_length=MAX_ROLES)
    reason: str | None = Field(default=None, pattern=_CODE, max_length=80)


class DailySummary(_View):
    """One workspace's day, as saved (``daily_summaries.facts``) and shown."""

    version: Literal[1] = SUMMARY_VERSION
    workspace: str = Field(pattern=_SLUG)
    name: str = Field(min_length=1, max_length=80)
    day: date
    made_at: AwareDatetime
    window_start: AwareDatetime
    health: Health
    unread: tuple[Unread, ...] = Field(max_length=MAX_SOURCES + 4)
    sources: tuple[SourceFact, ...] = Field(max_length=MAX_SOURCES)
    sample_left_out: tuple[str, ...] = Field(max_length=MAX_SOURCES)
    changes: tuple[Change, ...] = Field(max_length=MAX_CHANGES)
    compared_with: date | None = None
    alerts: AlertsFact | None = None
    agents: AgentsFact | None = None
    scout: ScoutFact | None = None

    @property
    def nothing_to_report(self) -> bool:
        """True when nothing at all was read: then it says so, never "0"."""
        return not self.sources and self.alerts is None and self.agents is None and self.scout is None


def summarise(  # noqa: PLR0913  # the workspace, its cards, and each other fact, then the window
    workspace: str,
    name: str,
    detail: ProjectDetail | None,
    *,
    day: date,
    made_at: datetime,
    window_start: datetime,
    alerts: AlertsFact | None = None,
    agents: AgentsFact | None = None,
    scout: ScoutFact | None = None,
    not_read: Sequence[Unread] = (),
    previous: DailySummary | None = None,
) -> DailySummary:
    """The workspace's summary for ``day``, from facts code already holds."""
    unread: list[Unread] = []
    sources: list[SourceFact] = []
    sample: list[str] = []
    for card in detail.cards if detail is not None else ():
        if card.sample:
            sample.append(card.title)
        elif not card.freshness.usable:
            unread.append(Unread(title=card.title, reason=_why_unread(card)))
        else:
            sources.append(_fact(card))
    unread += not_read
    earlier = previous if previous is not None and previous.day < day else None
    return DailySummary(
        workspace=workspace,
        name=name,
        day=day,
        made_at=made_at,
        window_start=window_start,
        health=_health(sources, unread, alerts),
        unread=tuple(unread[: MAX_SOURCES + 4]),
        sources=tuple(sources[:MAX_SOURCES]),
        sample_left_out=tuple(sample[:MAX_SOURCES]),
        changes=_changes(earlier, sources),
        compared_with=earlier.day if earlier is not None else None,
        alerts=alerts,
        agents=agents,
        scout=scout,
    )


def read_summary(facts: object) -> DailySummary | None:
    """A saved summary read back, or ``None`` if it no longer reads as one (shown as unreadable)."""
    try:
        return DailySummary.model_validate(facts)
    except ValidationError:
        return None


def _why_unread(card: Card) -> str:
    """The card's own reason code when it failed, else how old its data is."""
    for metric in card.metrics:
        if isinstance(metric.value, NotKnown) and not metric.value.reason.startswith("freshness."):
            return metric.value.reason
    return f"freshness.{card.freshness.value}"


def _fact(card: Card) -> SourceFact:
    flagged = tuple(entry for entry in card.entries if entry.tone in _FLAGGED)
    return SourceFact(
        card=card.id,
        title=card.title,
        metrics=card.metrics[:MAX_METRICS],
        flagged=flagged[:MAX_LINES],
    )


def _health(sources: Sequence[SourceFact], unread: Sequence[Unread], alerts: AlertsFact | None) -> Health:
    """Worst of what was read; nothing read means unknown, never "all fine"."""
    tones = {entry.tone for source in sources for entry in source.flagged}
    if Tone.ALERT in tones:
        return Health.ALERT
    if not sources:
        return Health.UNKNOWN
    if Tone.WARN in tones or unread or (alerts is not None and alerts.opened > alerts.closed):
        return Health.WARN
    return Health.OK


def _changes(previous: DailySummary | None, sources: Sequence[SourceFact]) -> tuple[Change, ...]:
    if previous is None:
        return ()
    before = {(s.card, m.label): m.value for s in previous.sources for m in s.metrics}
    found: list[Change] = []
    for source in sources:
        for metric in source.metrics:
            old = before.get((source.card, metric.label))
            if old is None or isinstance(old, NotKnown) or isinstance(metric.value, NotKnown):
                continue
            if old != metric.value:
                found.append(Change(title=source.title, label=metric.label, before=old, after=metric.value))
    return tuple(found[:MAX_CHANGES])


def _count(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one}" if count == 1 else f"{count} {many or one + 's'}"


def first_line(summary: DailySummary) -> str:
    """The summary in one line, from counts and our own words (the Board card and the briefing)."""
    parts: list[str] = []
    if not summary.sources and not summary.unread:
        # Nothing real was read: say so, never just "unknown" (and never 0).
        sample = len(summary.sample_left_out)
        left_out = f" ({_count(sample, 'sample card')} left out)" if sample else ""
        parts.append(f"Nothing to report: no real source is connected yet{left_out}")
    else:
        if summary.unread:
            titles = ", ".join(u.title for u in summary.unread[:3])
            more = f" and {len(summary.unread) - 3} more" if len(summary.unread) > 3 else ""
            parts.append(f"Could not read {titles}{more}")
        parts.append(_HEALTH_WORDS[summary.health])
    flagged = sum(len(s.flagged) for s in summary.sources)
    if flagged:
        parts.append(_count(flagged, "thing to look at", "things to look at"))
    if summary.alerts is not None and (summary.alerts.opened or summary.alerts.closed):
        parts.append(f"{_count(summary.alerts.opened, 'alert')}, {summary.alerts.closed} back up")
    if summary.changes:
        parts.append(_count(len(summary.changes), "number changed", "numbers changed"))
    if summary.agents is not None and summary.agents.agents:
        parts.append(f"{summary.agents.on} of {_count(summary.agents.agents, 'agent')} on")
    if summary.scout is not None and summary.scout.state == "done":
        parts.append(f"scout recommends {_count(len(summary.scout.recommended), 'role')}")
    return "; ".join(parts) + "."


def briefing(
    summaries: Sequence[DailySummary], *, day: date, local_time: str, late: bool
) -> tuple[str, tuple[str, ...]]:
    """The Board's morning message: a title and its lines (one per workspace, worst first), no links."""
    title = f"Yaqzan briefing, {day.strftime('%a')} {day.day} {day.strftime('%b')}"
    lines: list[str] = []
    if late:
        lines.append(f"Made late, at {local_time}: the dashboard was not running at the usual time.")
    if summaries:
        needs = [s for s in summaries if s.health in {Health.ALERT, Health.WARN} or s.unread]
        lines.append(f"{_count(len(summaries), 'project')}; {len(needs)} need a look.")
    else:
        lines.append("No project has a summary today.")
    end = "Details on the dashboard."
    used = sum(_bytes(line) for line in lines) + _bytes(end)
    for index, summary in enumerate(_worst_first(summaries)):
        line = f"{summary.name}: {first_line(summary)}"
        if used + _bytes(line) > MAX_MESSAGE_BYTES - 60:  # room left for the "more" line
            lines.append(f"... and {_count(len(summaries) - index, 'more project')}.")
            break
        lines.append(line)
        used += _bytes(line)
    lines.append(end)
    return title, tuple(lines)


def _bytes(line: str) -> int:
    return len(line.encode()) + 1  # and its line break


def _worst_first(summaries: Sequence[DailySummary]) -> list[DailySummary]:
    order = {Health.ALERT: 0, Health.WARN: 1, Health.UNKNOWN: 2, Health.OK: 3}
    return sorted(summaries, key=lambda s: (order[s.health], 0 if s.unread else 1))

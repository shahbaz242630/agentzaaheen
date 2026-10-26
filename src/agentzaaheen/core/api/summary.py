"""A workspace's daily summary and the Board's morning briefing, written by code (F17 slice 6, ADR-039).

Every fact comes from what Agent Zaaheen already holds (PRD F2: every number is put
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
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from agentzaaheen.core.api.views import Card, Entry, Health, Metric, NotKnown, ProjectDetail, Tone, Value

SUMMARY_VERSION: Literal[1] = 1
MAX_SOURCES = 12
MAX_METRICS = 6
MAX_LINES = 5
MAX_CHANGES = 8
MAX_ROLES = 40
MAX_MISSED = 10  # runs that did not run, kept by name in a summary; more are only counted
MISSED_SHOWN = 3  # of those, named in the one-line summary
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


class MissedFact(_View):
    """One agent run that did not run in the window: its role and why (a code, F11)."""

    role: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=64)
    reason: str = Field(pattern=_CODE, max_length=80)


class SkippedFact(_View):
    """Runs skipped as not meant to run (off, paused, stopped): one role and reason, counted."""

    role: str | None = Field(default=None, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=64)
    reason: str = Field(pattern=_CODE, max_length=80)
    count: int = Field(ge=1)


class StopFact(_View):
    """A stop switch that is on: for everything, one project (by name) or one agent, and since when."""

    scope: Literal["all", "project", "agent"]
    name: str | None = Field(default=None, min_length=1, max_length=80)
    since: AwareDatetime | None = None


class AgentsFact(_View):
    """The workspace's agents now: how many, how many on, spend this month against its cap, and runs missed.

    ``missed`` names up to :data:`MAX_MISSED` runs in the window that did not run (refused, crashed,
    over time, interrupted); ``missed_count`` counts them all. ``skipped`` counts the runs not started
    because they were not meant to run, by role and reason (Chairman, 26 Oct 2026: one line, never one
    per run). Summaries saved before F11 have none of these.
    """

    agents: int = Field(ge=0)
    on: int = Field(ge=0)
    workspace_on: bool
    spent_minor: int = Field(ge=0)
    cap_minor: int | None = Field(default=None, ge=0)
    missed: tuple[MissedFact, ...] = Field(default=(), max_length=MAX_MISSED)
    missed_count: int = Field(default=0, ge=0)
    skipped: tuple[SkippedFact, ...] = Field(default=(), max_length=MAX_MISSED)
    skipped_count: int = Field(default=0, ge=0)


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
    if summary.agents is not None and summary.agents.missed_count:
        parts.append(_missed(summary.agents))
    if summary.agents is not None and summary.agents.skipped_count:
        parts.append(_skipped(summary.agents))
    if summary.scout is not None and summary.scout.state == "done":
        parts.append(f"scout recommends {_count(len(summary.scout.recommended), 'role')}")
    return "; ".join(parts) + "."


def briefing(  # noqa: PLR0913  # the summaries, the day and time, then each kind of extra line
    summaries: Sequence[DailySummary],
    *,
    day: date,
    local_time: str,
    late: bool,
    keys: Sequence[str] = (),
    claude: Sequence[str] = (),
    stops: Sequence[str] = (),
) -> tuple[str, tuple[str, ...]]:
    """The Board's morning message: a title and its lines (one per workspace, worst first), no links.

    ``keys`` are the keys that end within 14 days or have ended (``tools.key_dates``), and
    ``claude`` the Board's Claude lines (runs that did not run, caps at 80%), written by
    ``llm_gateway.claude_lines`` from fixed templates; both come right after the count line, keys first.
    ``stops`` are the stop switches that are on (:func:`stop_lines`): they come first of all.
    """
    title = f"Agent Zaaheen briefing, {day.strftime('%a')} {day.day} {day.strftime('%b')}"
    lines: list[str] = []
    if late:
        lines.append(f"Made late, at {local_time}: the dashboard was not running at the usual time.")
    if summaries:
        needs = [
            s for s in summaries if s.health in {Health.ALERT, Health.WARN} or s.unread or _any_missed(s)
        ]
        lines.append(f"{_count(len(summaries), 'project')}; {len(needs)} need a look.")
    else:
        lines.append("No project has a summary today.")
    lines.extend(stops)
    lines.extend(keys)
    lines.extend(claude)
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


def _missed(agents: AgentsFact) -> str:
    """'2 agent runs did not run (monitoring: interrupted; sales: agent cap used)': codes in plain words."""
    shown = agents.missed[:MISSED_SHOWN]
    named = "; ".join(f"{m.role}: {m.reason.removeprefix('run.').replace('_', ' ')}" for m in shown)
    rest = agents.missed_count - len(shown)
    more = f"; and {rest} more" if rest > 0 and named else ""
    runs = _count(agents.missed_count, "agent run")
    return f"{runs} did not run ({named}{more})" if named else f"{runs} did not run"


def _any_missed(summary: DailySummary) -> bool:
    return summary.agents is not None and summary.agents.missed_count > 0


# The reasons a run was skipped, in our own words (codes from ``policy.SKIPPED_REASONS``).
SKIP_WORDS = {
    "run.stop_all": "stop-all switch on",
    "run.workspace_frozen": "project stopped",
    "run.agent_paused": "paused",
    "run.agent_off": "switched off",
    "run.workspace_off": "project switched off",
    "run.no_agent": "agent removed",
}


def _skipped(agents: AgentsFact) -> str:
    """'2 agent runs skipped (monitoring: paused)': one line, by role and reason, never one per run."""
    shown = agents.skipped[:MISSED_SHOWN]
    named = "; ".join(
        f"{s.role or 'an agent'}: {SKIP_WORDS.get(s.reason, s.reason.removeprefix('run.').replace('_', ' '))}"
        for s in shown
    )
    more = "; and more" if len(agents.skipped) > len(shown) else ""
    return f"{_count(agents.skipped_count, 'agent run')} skipped ({named}{more})"


def stop_lines(stops: Sequence[StopFact], zone: ZoneInfo) -> tuple[str, ...]:
    """The briefing's stop switch lines: every switch that is on, always (Chairman, 26 Oct 2026)."""
    lines: list[str] = []
    for stop in stops:
        if stop.scope == "all":
            lines.append(f"Stop switch ON: all agents stopped{_since(stop, zone)}.")
        elif stop.scope == "project":
            lines.append(f"Stop switch ON: {stop.name or 'a project'} stopped{_since(stop, zone)}.")
    paused = [s for s in stops if s.scope == "agent"]
    if paused:
        known = [s for s in paused if s.since is not None]
        oldest = f" (the oldest{_since(min(known, key=_when), zone)})" if known else ""
        lines.append(f"Stop switch ON: {_count(len(paused), 'agent')} paused{oldest}.")
    return tuple(lines)


def _when(stop: StopFact) -> datetime:
    return stop.since or datetime.max.replace(tzinfo=ZoneInfo("UTC"))


def _since(stop: StopFact, zone: ZoneInfo) -> str:
    if stop.since is None:
        return ""
    at = stop.since.astimezone(zone)
    return f" since {at.day} {at.strftime('%b %H:%M')}"

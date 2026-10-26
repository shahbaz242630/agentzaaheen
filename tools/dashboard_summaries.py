"""Daily summaries and the morning briefing on this laptop (F17 slice 6, ADR-039).

Split from ``tools.dashboard``. While the dashboard runs, :class:`LaptopSummaries`
checks once a minute what is due in the Chairman's time zone
(``core.scheduler.plan``):

1. **05:00, gather:** each active workspace's summary is written by code from
   what the dashboard holds (its cards, the phone alerts counted by
   :class:`AlertLog`, its agents and the scout's newest advice) and saved;
   an archived workspace is left out.
2. **07:00, brief:** one Board message is built from today's summaries, saved,
   then sent to the Chairman's phone through ntfy. If it cannot be sent, why
   is saved and it is tried again every 15 minutes that day. Once it is
   sent, the briefing's Healthchecks check is pinged (``--briefing-ping-env``),
   so a briefing that never arrives (the laptop was off) alerts the phone too.

A dashboard started later the same day catches up once, and says it is late.
Nothing here asks a model, switches anything on or spends.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from pydantic import SecretStr, ValidationError

from agentzaaheen.core.api import Portfolio
from agentzaaheen.core.api.agents import WorkspaceAgentsOut
from agentzaaheen.core.api.summary import (
    AgentsFact,
    AlertsFact,
    DailySummary,
    ScoutFact,
    Unread,
    briefing,
    read_summary,
    summarise,
)
from agentzaaheen.core.api.workspaces import WorkspaceDirectory
from agentzaaheen.core.llm_gateway.scout import ScoutReport
from agentzaaheen.core.net import HeartbeatClient, PushClient
from agentzaaheen.core.notify import Alert
from agentzaaheen.core.policy import SummaryControl
from agentzaaheen.core.scheduler import Plan, local_day, plan, window_start
from agentzaaheen.core.scheduler.briefing import Briefed
from agentzaaheen.core.sdk import SourceError, get_logger, say
from agentzaaheen.core.store import BriefingRow, ScoutRun, StoreError, SummaryRow, WorkspaceRow
from tools.key_dates import KeyDate, key_lines

CHECK_EVERY_S = 60.0
RETRY_AFTER = timedelta(minutes=15)
KEEP_ALERTS = timedelta(days=3)
NO_TOPIC = "briefing.no_topic"
UNMAKEABLE = "summary.not_valid"  # a fact broke the summary's own limits (a bug: nothing is guessed)

Clock = Callable[[], datetime]
Agents = Callable[[WorkspaceRow], Awaitable[WorkspaceAgentsOut]]
Scout = Callable[[WorkspaceRow], Awaitable[ScoutRun | None]]
# The Board's Claude lines for a briefing made at a given time (F10 slice 3a).
ClaudeLines = Callable[[datetime], Awaitable[tuple[str, ...]]]


class AlertLog:
    """The phone alerts the uptime watcher sent, by project, kept in memory since the dashboard started.

    The watcher runs in its own thread, so adding and counting take a lock.
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.started_at = clock()
        self._lock = threading.Lock()
        self._seen: list[tuple[datetime, str, bool]] = []

    def add(self, alerts: Sequence[Alert]) -> None:
        """Remember these alerts (down or back up) at the time now."""
        now = self._clock()
        with self._lock:
            self._seen = [seen for seen in self._seen if seen[0] >= now - KEEP_ALERTS]
            self._seen += [(now, alert.project_id, alert.down) for alert in alerts]

    def count(self, project: str, since: datetime) -> AlertsFact:
        """How many went down and came back up for ``project`` since ``since`` (or since counting began)."""
        with self._lock:
            mine = [down for at, seen, down in self._seen if seen == project and at >= since]
        return AlertsFact(
            opened=sum(mine), closed=len(mine) - sum(mine), counted_since=max(since, self.started_at)
        )


class LaptopSummaries:
    """Makes, keeps and sends the summaries and briefings; the dashboard's summary routes read through it."""

    def __init__(  # noqa: PLR0913  # the store, the facts' sources, the phone, the clock and the zone
        self,
        *,
        control: Callable[[], Awaitable[SummaryControl]],
        portfolio: Portfolio,
        directory: WorkspaceDirectory,
        clock: Clock,
        zone: ZoneInfo,
        agents: Agents | None = None,
        scout: Scout | None = None,
        claude: ClaudeLines | None = None,
        key_dates: Sequence[KeyDate] = (),
        alerts: AlertLog | None = None,
        watched: frozenset[str] = frozenset(),
        topic: SecretStr | None = None,
        ping: SecretStr | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        check_every_s: float = CHECK_EVERY_S,
    ) -> None:
        self._control = control
        self._portfolio = portfolio
        self._directory = directory
        self._clock = clock
        self._zone = zone
        self._agents = agents
        self._scout = scout
        self._claude = claude
        self._key_dates = tuple(key_dates)
        self._alerts = alerts
        self._watched = watched
        self._topic = topic
        self._ping = ping
        self._transport = transport
        self._check_every_s = check_every_s
        self._gathered: date | None = None
        self._next_try: datetime | None = None
        self._task: asyncio.Task[None] | None = None
        self._log = get_logger("agentzaaheen.summaries")

    # ---- what the routes read

    def _today(self) -> date:
        return local_day(self._clock(), self._zone)

    async def newest(self) -> Mapping[str, SummaryRow]:
        """Each active workspace's newest summary, by its address (a workspace with none is left out)."""
        control, today = await self._control(), self._today()
        found: dict[str, SummaryRow] = {}
        for workspace in self._directory.all():
            rows = await control.recent(workspace, until=today, days=1)
            if rows:
                found[workspace.slug] = rows[0]
        return found

    async def recent(self, workspace: WorkspaceRow) -> Sequence[SummaryRow]:
        """One workspace's last 7 days, newest first."""
        return await (await self._control()).recent(workspace, until=self._today(), days=7)

    async def briefings(self) -> Sequence[BriefingRow]:
        """The Board's last 7 briefings, newest first."""
        return await (await self._control()).briefings(until=self._today(), days=7)

    async def make_now(self) -> tuple[int, int]:
        """Make today's summaries again for every active workspace (nothing is sent)."""
        now = self._clock()
        day = local_day(now, self._zone)
        return await self._make(day, window_start(day, self._zone), only_missing=False)

    # ---- the morning job

    async def start(self) -> None:
        """Start checking once a minute (inside the web server's event loop)."""
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        """Stop checking (the dashboard is stopping)."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001  # a failed check must never end the job
                say(self._log, "briefing_job_failed", kind=type(exc).__name__)
            await asyncio.sleep(self._check_every_s)

    async def tick(self) -> Plan | None:
        """One check: gather today's summaries and send today's briefing, if due (``None``: waiting)."""
        now = self._clock()
        day = local_day(now, self._zone)
        if self._next_try is not None and now < self._next_try:
            return None  # a try failed: wait before the next
        self._next_try = None
        control = await self._control()
        briefed = await self._briefed(control, day)
        due = plan(now, self._zone, gathered=self._gathered == day, briefed=briefed)
        if due.gather:
            _made, failed = await self._make(day, due.window_start, only_missing=True)
            if failed:
                self._next_try = now + RETRY_AFTER
                return due
            self._gathered = day
        if due.send:
            await self._send(control, due, briefed)
        return due

    async def _briefed(self, control: SummaryControl, day: date) -> Briefed:
        rows = await control.briefings(until=day, days=1)
        return rows[0].state if rows and rows[0].day == day else "none"

    async def _make(self, day: date, start: datetime, *, only_missing: bool) -> tuple[int, int]:
        control = await self._control()
        made = failed = 0
        for workspace in self._directory.all():
            try:
                if only_missing and await _has_day(control, workspace, day):
                    continue
                previous = await control.before(workspace, day)
                summary = await self._summary(workspace, day, start, previous)
                await control.save(workspace, day, summary.model_dump(mode="json"))
            except (StoreError, ValidationError) as exc:
                code = exc.code.value if isinstance(exc, StoreError) else UNMAKEABLE
                say(self._log, "summary_failed", workspace=workspace.slug, code=code)
                failed += 1
                continue
            made += 1
        say(self._log, "summaries_made", entries=made, failing=failed)
        return made, failed

    async def _summary(
        self, workspace: WorkspaceRow, day: date, start: datetime, previous: SummaryRow | None
    ) -> DailySummary:
        not_read: list[Unread] = []
        agents = await self._agents_fact(workspace, not_read)
        scout = await self._scout_fact(workspace, not_read)
        alerts = (
            self._alerts.count(workspace.slug, start)
            if self._alerts is not None and workspace.slug in self._watched
            else None
        )
        return summarise(
            workspace.slug,
            workspace.name,
            self._portfolio.project(workspace.slug),
            day=day,
            made_at=self._clock(),
            window_start=start,
            alerts=alerts,
            agents=agents,
            scout=scout,
            not_read=not_read,
            previous=read_summary(previous.facts) if previous is not None else None,
        )

    async def _agents_fact(self, workspace: WorkspaceRow, not_read: list[Unread]) -> AgentsFact | None:
        if self._agents is None:
            return None
        try:
            seen = await self._agents(workspace)
        except StoreError as exc:
            not_read.append(Unread(title="Agents", reason=exc.code.value))
            return None
        return AgentsFact(
            agents=len(seen.agents),
            on=sum(1 for agent in seen.agents if agent.enabled),
            workspace_on=seen.enabled,
            spent_minor=seen.spent_minor,
            cap_minor=seen.cap_minor,
        )

    async def _scout_fact(self, workspace: WorkspaceRow, not_read: list[Unread]) -> ScoutFact | None:
        if self._scout is None:
            return None
        try:
            run = await self._scout(workspace)
        except StoreError as exc:
            not_read.append(Unread(title="Scout", reason=exc.code.value))
            return None
        if run is None:
            return None
        if run.state != "done":
            return ScoutFact(state=run.state, at=run.finished_at or run.started_at, reason=run.reason)
        try:
            report = ScoutReport.model_validate(run.result)
        except ValidationError:
            not_read.append(Unread(title="Scout", reason="scout.unreadable"))
            return None
        recommended = tuple(advice.role_id for advice in report.roles if advice.recommend)
        return ScoutFact(state="done", at=run.finished_at or run.started_at, recommended=recommended[:40])

    async def _claude_lines(self) -> tuple[str, ...]:
        """The Board's Claude lines; if they cannot be read, the briefing says so (never left out)."""
        if self._claude is None:
            return ()
        try:
            return await self._claude(self._clock())
        except StoreError as exc:
            return (f"Claude runs could not be read ({exc.code.value}); see the dashboard.",)

    async def _send(self, control: SummaryControl, due: Plan, briefed: Briefed) -> None:
        if briefed == "none":
            summaries = []
            for workspace in self._directory.all():
                rows = await control.recent(workspace, until=due.day, days=1)
                found = read_summary(rows[0].facts) if rows and rows[0].day == due.day else None
                if found is not None:
                    summaries.append(found)
            title, lines = briefing(
                summaries,
                day=due.day,
                local_time=due.local_time,
                late=due.late,
                keys=key_lines(self._key_dates, due.day),
                claude=await self._claude_lines(),
            )
            await control.open_briefing(due.day, title=title, message="\n".join(lines))
        saved = (await control.briefings(until=due.day, days=1))[0]
        if self._topic is None:
            if saved.reason != NO_TOPIC:
                await control.briefing_not_sent(due.day, NO_TOPIC)
            self._next_try = _next_day(self._clock(), self._zone)  # nothing can send it today
            return
        try:
            async with PushClient(self._topic, transport=self._transport) as pusher:
                await pusher.send_lines(saved.title, saved.message.split("\n"))
        except SourceError as exc:
            await control.briefing_not_sent(due.day, exc.reason_code)
            self._next_try = self._clock() + RETRY_AFTER
            return
        await control.briefing_sent(due.day)
        await self._heartbeat()

    async def _heartbeat(self) -> None:
        """Tell Healthchecks today's briefing went out; a failure is logged once and left to its alert."""
        if self._ping is None:
            return
        try:
            async with HeartbeatClient(self._ping, transport=self._transport) as beat:
                await beat.ping("briefing")
        except SourceError:
            return  # the client said why; never retried: a missing ping makes Healthchecks alert the phone


async def _has_day(control: SummaryControl, workspace: WorkspaceRow, day: date) -> bool:
    rows = await control.recent(workspace, until=day, days=1)
    return bool(rows) and rows[0].day == day


def _next_day(now: datetime, zone: ZoneInfo) -> datetime:
    """Midnight starting the Chairman's next day."""
    local = now.astimezone(zone)
    return datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tzinfo=zone)

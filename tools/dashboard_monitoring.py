"""The Monitoring agent on this machine: every 5 minutes, through the runner (F11 slices 4-5).

Each round, every workspace's Monitoring agents that are switched on are run
through :meth:`~agentzaaheen.core.runner.Runner.run`, which checks the stop
switches, the agent's switch and caps, its frozen role pack and its tools
before anything runs. The agent reads the uptime watch's newest check
(``uptime.read``) and the newest Healthchecks read (``healthchecks.read``,
slice 5a): it never reaches the network itself. It compares each with its
bookmark and writes a finding only when something changed; the runner saves
the findings and moves the bookmarks together.

Healthchecks is read by code, once per round (one GET for every project), and
only when the round has an agent to wake. Without a Healthchecks list on this
machine, the agents leave Healthchecks out (the start-up line says so).

An agent that is switched off is not woken (the runner would only count it
as skipped 288 times a day). A stop switch still stops a woken agent, and the
briefing names the switch. The round runs inside the web server's event loop,
like the morning job, so it shares the database pool.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Protocol

from agentzaaheen.core.api.workspaces import WorkspaceDirectory
from agentzaaheen.core.policy import Budget
from agentzaaheen.core.runner import AgentResult, FindingDraft, Runner, RunTicket
from agentzaaheen.core.sdk import (
    DataClass,
    HeartbeatReport,
    ToolLevel,
    ToolSpec,
    Trust,
    UptimeReport,
    get_logger,
    say,
)
from agentzaaheen.core.store import AgentRow, StoreError, WorkspaceRow
from agentzaaheen.plugins.agents.monitoring import (
    BOOKMARK,
    HEARTBEATS_BOOKMARK,
    Note,
    watch,
    watch_heartbeats,
)

ROLE = "monitoring"
EVERY = timedelta(minutes=5)  # the role pack: "wakes every 5 minutes"
UPTIME_READ = "uptime.read"
HEALTHCHECKS_READ = "healthchecks.read"
MONITORING_TOOLS: Mapping[str, ToolSpec] = {
    UPTIME_READ: ToolSpec(
        name=UPTIME_READ,
        level=ToolLevel.READ,
        output_data_class=DataClass.INTERNAL,
        description="The uptime watch's newest check of one project: status codes and timings only.",
    ),
    HEALTHCHECKS_READ: ToolSpec(
        name=HEALTHCHECKS_READ,
        level=ToolLevel.READ,
        output_data_class=DataClass.INTERNAL,
        description="The newest Healthchecks read: each check's short name, status and last ping only.",
    ),
}
# Code only: one step, no model tokens (the gate needs at least 1), and a minute is plenty.
MONITORING_BUDGET = Budget(steps=1, tokens=1, seconds=60)

Clock = Callable[[], datetime]


class MonitoringWork:
    """One Monitoring run for one project: its newest uptime and Healthchecks reads against the bookmarks."""

    role_id = ROLE
    tools = frozenset({UPTIME_READ, HEALTHCHECKS_READ})
    budget = MONITORING_BUDGET

    def __init__(  # noqa: PLR0913  # the project, both reads, what is listed for it, and the clock
        self,
        project: str,
        name: str,
        report: UptimeReport | None,
        *,
        listed: bool,
        clock: Clock,
        heartbeats: HeartbeatReport | None = None,
        slugs: tuple[str, ...] | None = None,
    ) -> None:
        """``slugs``: the project's Healthchecks checks (``None``: no Healthchecks list here)."""
        self._project = project
        self._name = name
        self._report = report
        self._listed = listed
        self._clock = clock
        self._heartbeats = heartbeats
        self._slugs = slugs

    async def run(self, ticket: RunTicket) -> AgentResult:
        """Write a finding only where the project's state changed since its bookmark."""
        now = self._clock()
        uptime = watch(
            self._project,
            self._name,
            self._report,
            listed=self._listed,
            now=now,
            seen=ticket.bookmarks.get(BOOKMARK),
        )
        watched = {BOOKMARK: uptime}
        if self._slugs is not None:
            watched[HEARTBEATS_BOOKMARK] = watch_heartbeats(
                self._project,
                self._name,
                self._heartbeats,
                slugs=self._slugs,
                now=now,
                seen=ticket.bookmarks.get(HEARTBEATS_BOOKMARK),
            )
        notes = [each.note for each in watched.values() if each.note is not None]
        return AgentResult(
            findings=tuple(_finding(note) for note in notes),
            bookmarks={name: each.state for name, each in watched.items()},
        )


def _finding(note: Note) -> FindingDraft:
    return FindingDraft(
        kind=note.kind,
        severity=note.severity,
        title=note.title,
        body=note.body,
        evidence=note.evidence,
        trust=Trust.UNTRUSTED,  # what outside servers answered
        data_class=DataClass.INTERNAL,
    )


class HeartbeatReader(Protocol):
    """Reads Healthchecks once per round (``tools.dashboard_healthchecks.HealthchecksReader``)."""

    latest: HeartbeatReport | None

    async def refresh(self) -> None:
        """Read once; a failure keeps the last read."""
        ...


class LaptopMonitoring:
    """Wakes every workspace's Monitoring agents each ``every_s`` seconds, inside the server's event loop."""

    def __init__(  # noqa: PLR0913  # the runner, the agents, the workspaces, the reads, the clock, how often
        self,
        *,
        runner: Callable[[], Awaitable[Runner]],
        agents: Callable[[WorkspaceRow, str], Awaitable[Sequence[AgentRow]]],
        directory: WorkspaceDirectory,
        latest: Callable[[], Mapping[str, UptimeReport]],
        listed: frozenset[str],
        clock: Clock,
        heartbeats: HeartbeatReader | None = None,
        checks: Mapping[str, tuple[str, ...]] | None = None,
        every_s: float = EVERY.total_seconds(),
    ) -> None:
        self._runner = runner
        self._agents = agents
        self._directory = directory
        self._latest = latest
        self._listed = listed
        self._clock = clock
        self._heartbeats = heartbeats
        self._checks = dict(checks or {})
        self._every_s = every_s
        self._task: asyncio.Task[None] | None = None
        self._log = get_logger("agentzaaheen.monitoring")

    async def round(self) -> int:
        """Run every switched-on Monitoring agent once; how many runs were started."""
        runner = await self._runner()
        woken: list[tuple[WorkspaceRow, AgentRow]] = []
        for workspace in self._directory.all():
            try:
                agents = await self._agents(workspace, ROLE)
            except StoreError as exc:
                say(self._log, "monitoring_agents_unreadable", project=workspace.slug, code=exc.code.value)
                continue
            woken.extend((workspace, agent) for agent in agents)
        if not woken:
            return 0
        heartbeats: HeartbeatReport | None = None
        if self._heartbeats is not None:
            await self._heartbeats.refresh()  # once per round, for every project
            heartbeats = self._heartbeats.latest
        reports = dict(self._latest())  # swapped whole by the uptime watch: one consistent view
        started = 0
        for workspace, agent in woken:
            work = MonitoringWork(
                workspace.slug,
                workspace.name,
                reports.get(workspace.slug),
                listed=workspace.slug in self._listed,
                clock=self._clock,
                heartbeats=heartbeats,
                slugs=self._checks.get(workspace.slug, ()) if self._checks else None,
            )
            report = await runner.run(workspace.workspace_id, agent.agent_id, work)
            started += int(report.started)
        return started

    async def start(self) -> None:
        """Start the rounds (inside the web server's event loop)."""
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        """Stop the rounds."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._every_s)
            try:
                await self.round()
            except Exception as exc:  # noqa: BLE001  # a failed round must never end the rounds
                say(self._log, "monitoring_round_failed", kind=type(exc).__name__)

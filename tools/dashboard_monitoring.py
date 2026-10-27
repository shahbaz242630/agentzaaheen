"""The Monitoring agent on this machine: every 5 minutes, through the runner (F11 slice 4).

Each round, every workspace's Monitoring agents that are switched on are run
through :meth:`~agentzaaheen.core.runner.Runner.run`, which checks the stop
switches, the agent's switch and caps, its frozen role pack and its one tool
before anything runs. The agent reads the uptime watch's newest check (its
one tool, ``uptime.read``: it never reaches the network itself), compares it
with its bookmark and writes a finding only when something changed; the
runner saves the finding and moves the bookmark together.

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

from agentzaaheen.core.api.workspaces import WorkspaceDirectory
from agentzaaheen.core.policy import Budget
from agentzaaheen.core.runner import AgentResult, FindingDraft, Runner, RunTicket
from agentzaaheen.core.sdk import DataClass, ToolLevel, ToolSpec, Trust, UptimeReport, get_logger, say
from agentzaaheen.core.store import AgentRow, StoreError, WorkspaceRow
from agentzaaheen.plugins.agents.monitoring import BOOKMARK, watch

ROLE = "monitoring"
EVERY = timedelta(minutes=5)  # the role pack: "wakes every 5 minutes"
UPTIME_READ = "uptime.read"
MONITORING_TOOLS: Mapping[str, ToolSpec] = {
    UPTIME_READ: ToolSpec(
        name=UPTIME_READ,
        level=ToolLevel.READ,
        output_data_class=DataClass.INTERNAL,
        description="The uptime watch's newest check of one project: status codes and timings only.",
    )
}
# Code only: one step, no model tokens (the gate needs at least 1), and a minute is plenty.
MONITORING_BUDGET = Budget(steps=1, tokens=1, seconds=60)

Clock = Callable[[], datetime]


class MonitoringWork:
    """One Monitoring run for one project: the newest uptime check against the bookmark."""

    role_id = ROLE
    tools = frozenset({UPTIME_READ})
    budget = MONITORING_BUDGET

    def __init__(
        self, project: str, name: str, report: UptimeReport | None, *, listed: bool, clock: Clock
    ) -> None:
        self._project = project
        self._name = name
        self._report = report
        self._listed = listed
        self._clock = clock

    async def run(self, ticket: RunTicket) -> AgentResult:
        """Write a finding only when the project's state changed since the bookmark."""
        watched = watch(
            self._project,
            self._name,
            self._report,
            listed=self._listed,
            now=self._clock(),
            seen=ticket.bookmarks.get(BOOKMARK),
        )
        findings: tuple[FindingDraft, ...] = ()
        if watched.note is not None:
            note = watched.note
            findings = (
                FindingDraft(
                    kind=note.kind,
                    severity=note.severity,
                    title=note.title,
                    body=note.body,
                    evidence=note.evidence,
                    trust=Trust.UNTRUSTED,  # what outside servers answered
                    data_class=DataClass.INTERNAL,
                ),
            )
        return AgentResult(findings=findings, bookmarks={BOOKMARK: watched.state})


class LaptopMonitoring:
    """Wakes every workspace's Monitoring agents each ``every_s`` seconds, inside the server's event loop."""

    def __init__(  # noqa: PLR0913  # the runner, the agents, the workspaces, the checks, the clock, how often
        self,
        *,
        runner: Callable[[], Awaitable[Runner]],
        agents: Callable[[WorkspaceRow, str], Awaitable[Sequence[AgentRow]]],
        directory: WorkspaceDirectory,
        latest: Callable[[], Mapping[str, UptimeReport]],
        listed: frozenset[str],
        clock: Clock,
        every_s: float = EVERY.total_seconds(),
    ) -> None:
        self._runner = runner
        self._agents = agents
        self._directory = directory
        self._latest = latest
        self._listed = listed
        self._clock = clock
        self._every_s = every_s
        self._task: asyncio.Task[None] | None = None
        self._log = get_logger("agentzaaheen.monitoring")

    async def round(self) -> int:
        """Run every switched-on Monitoring agent once; how many runs were started."""
        runner = await self._runner()
        reports = dict(self._latest())  # swapped whole by the uptime watch: one consistent view
        started = 0
        for workspace in self._directory.all():
            try:
                agents = await self._agents(workspace, ROLE)
            except StoreError as exc:
                say(self._log, "monitoring_agents_unreadable", project=workspace.slug, code=exc.code.value)
                continue
            for agent in agents:
                work = MonitoringWork(
                    workspace.slug,
                    workspace.name,
                    reports.get(workspace.slug),
                    listed=workspace.slug in self._listed,
                    clock=self._clock,
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

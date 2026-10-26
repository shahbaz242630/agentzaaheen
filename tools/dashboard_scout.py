"""The scout on this laptop: reads a workspace's repository, asks the model, checks the answer, saves it.

Split from ``tools.dashboard`` (F17 slice 5b-2). ``LaptopScout.ask`` starts a
run (the database allows one per workspace) and returns at once; the work
runs in the background, **through the runner** as a built-in job (F11 slice 2):
stop switches, a start written down first, a time budget, and one run record
the morning briefing reads. Inside it:

1. **reading**: the repository bundle (slice 5a's reader) and what the
   project is built with (slice 5b-1), from GitHub, read-only;
2. **thinking**: the model's answer, through the runner's checked model: code
   checks it (``core.llm_gateway.scout``); a refused answer goes back with the
   reason, at most twice; then the run "needs a look";
3. the checked advice is saved.

Runs take turns (one model answer at a time on a CPU). A failure at any stage
is saved with its reason code and logged in plain English; nothing is guessed.
Stopping the dashboard cancels a run half way; the next ask closes it as
interrupted.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

import httpx
from pydantic import SecretStr

from agentzaaheen.core.api.scout import Asked
from agentzaaheen.core.llm_gateway import ChatModel
from agentzaaheen.core.llm_gateway.scout import (
    INSTRUCTIONS_VERSION,
    REFUSAL_TELLS,
    SCOUT_TEMPERATURE,
    SCOUT_TOKENS,
    RoleBrief,
    ScoutError,
    ScoutReport,
    check_answer,
    scout_messages,
)
from agentzaaheen.core.policy import Budget, Channel, Role, ScoutControl, ScoutVerdict
from agentzaaheen.core.registry.roles import RoleLibrary
from agentzaaheen.core.runner import (
    MAX_RETRIES,
    AgentFailedError,
    AgentResult,
    RunCode,
    Runner,
    RunTicket,
    ShapeRefusal,
)
from agentzaaheen.core.sdk import RepoBundle, RepoProfile, RunOutcome, SourceError, get_logger, say
from agentzaaheen.core.store import ScoutRun, StoreError, WorkspaceRow
from agentzaaheen.plugins.connectors.github import GitHubFiles
from tools.dashboard_adding import with_github
from tools.dashboard_profile import LaptopProfiles, Repos

CRASHED = "scout.crashed"
# Reading and up to three answers of about 5 minutes each on the server's CPU (F11: retries).
SCOUT_BUDGET = Budget(steps=1 + MAX_RETRIES, tokens=(1 + MAX_RETRIES) * SCOUT_TOKENS, seconds=1500)


class LaptopScout:
    """The scout's backend: starts runs and does their work in the background, one at a time."""

    def __init__(  # noqa: PLR0913  # the rules, the runner, each repo, GitHub, the model, the roles, more facts
        self,
        *,
        control: Callable[[], Awaitable[ScoutControl]],
        runner: Callable[[], Awaitable[Runner]],
        repos: Repos,
        token: SecretStr | None,
        model: ChatModel | None,
        model_id: str,
        library: RoleLibrary,
        clock: Callable[[], datetime],
        profiles: LaptopProfiles | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._control = control
        self._runner = runner
        self._clock = clock
        self._repos = repos
        self._token = token
        self._model = model
        self._model_id = model_id
        self._roles = tuple(RoleBrief(p.id, p.title, p.mission) for p in library.packs)
        self._profiles = profiles
        self._transport = transport
        self._turns = asyncio.Lock()  # one model answer at a time
        self._running: set[asyncio.Task[None]] = set()
        self._log = get_logger("agentzaaheen.scout")

    @property
    def connected(self) -> bool:
        """True if the scout can run: a GitHub token, a model and at least one role."""
        return self._token is not None and self._model is not None and bool(self._roles)

    async def ask(self, workspace: WorkspaceRow) -> Asked:
        """Start a run in the background; say whether it started."""
        repo = (await self._repos()).get(workspace.slug)
        if repo is None:
            return "scout.no_repo"
        control = await self._control()
        verdict, run_id = await control.start(
            workspace, repo=repo, model=self._model_id, role=Role.CHAIRMAN, channel=Channel.DASHBOARD
        )
        if verdict is ScoutVerdict.STARTED and run_id is not None:
            task = asyncio.create_task(self._work(control, workspace, run_id, repo))
            self._running.add(task)
            task.add_done_callback(self._running.discard)
        return _ASKED[verdict]

    async def latest(self, workspace: WorkspaceRow) -> ScoutRun | None:
        """The workspace's newest run."""
        return await (await self._control()).latest(workspace)

    async def wait(self) -> None:
        """Wait for every run started so far (tests, and a calm shutdown)."""
        await asyncio.gather(*self._running, return_exceptions=True)

    async def close(self) -> None:
        """Stop runs half way (the dashboard is stopping); the next ask closes them as interrupted."""
        for task in list(self._running):
            task.cancel()
        await self.wait()

    async def _work(self, control: ScoutControl, workspace: WorkspaceRow, run_id: UUID, repo: str) -> None:
        started = time.monotonic()
        work = _ScoutWork(self, control, workspace, run_id, repo)
        try:
            async with self._turns:
                runner = await self._runner()
                report = await runner.run_system(workspace.workspace_id, work, run_id=run_id)
        except StoreError as exc:  # the runner's own reads (stop switches) could not be made
            await self._failed(control, workspace, run_id, work.stage, exc.code.value)
            return
        except Exception:  # noqa: BLE001  # anything else still closes the run, with a code, never a trace
            await self._failed(control, workspace, run_id, work.stage, CRASHED)
            return
        if report.outcome is RunOutcome.OK:
            say(
                self._log,
                "scout_finished",
                workspace=workspace.slug,
                duration_ms=round((time.monotonic() - started) * 1000),
                entries=work.entries,
            )
            return
        # Needs a look keeps the last refusal's code, so the screen says what was wrong with the answer.
        code = report.detail or report.code or CRASHED
        await self._failed(
            control, workspace, run_id, work.stage, CRASHED if code == RunCode.CRASHED else code
        )

    @property
    def roles(self) -> tuple[RoleBrief, ...]:
        """The role library as the scout is told it."""
        return self._roles

    async def bundle(self, repo: str) -> RepoBundle:
        """The repository's paths and the files the reader picked (GitHub, read-only)."""
        return await with_github(
            self._token, self._transport, lambda http: GitHubFiles(http, repo).bundle(now=self._clock())
        )

    async def _profile(self, workspace: WorkspaceRow) -> RepoProfile | None:
        """What the project is built with, as extra facts; the scout still runs without them."""
        if self._profiles is None:
            return None
        try:
            checked = await self._profiles.profile(workspace)
        except SourceError:
            return None
        return None if checked is None else checked.profile

    async def _failed(
        self, control: ScoutControl, workspace: WorkspaceRow, run_id: UUID, stage: str, code: str
    ) -> None:
        say(self._log, "scout_failed", workspace=workspace.slug, stage=stage, code=code)
        with contextlib.suppress(
            StoreError
        ):  # the database is down too: the next ask closes it as interrupted
            await control.fail(workspace, run_id, code)


_ASKED: dict[ScoutVerdict, Asked] = {
    ScoutVerdict.STARTED: "scout.started",
    ScoutVerdict.BUSY: "scout.busy",
    ScoutVerdict.NOT_ALLOWED: "scout.not_allowed",
}


class _ScoutShape:
    """The scout's answer shape: the checked report, or a refusal the model is told about."""

    def __init__(self, bundle: RepoBundle, profile: RepoProfile | None, roles: tuple[RoleBrief, ...]) -> None:
        self._bundle = bundle
        self._profile = profile
        self._roles = roles

    def check(self, answer: str) -> ScoutReport:
        """The report, checked by code; a :class:`ShapeRefusal` with our own words if it is unusable."""
        try:
            return check_answer(answer, self._bundle, self._profile, self._roles)
        except ScoutError as exc:
            raise ShapeRefusal(exc.code, REFUSAL_TELLS.get(exc.code, "it did not pass the checks.")) from None


class _ScoutWork:
    """One scout run as the runner sees it: a built-in, read-only job with no tools."""

    role_id = "scout"
    tools: frozenset[str] = frozenset()
    budget = SCOUT_BUDGET
    version = INSTRUCTIONS_VERSION

    def __init__(  # the scout, its rules, where, which run, which repository
        self, scout: LaptopScout, control: ScoutControl, workspace: WorkspaceRow, run_id: UUID, repo: str
    ) -> None:
        self._scout = scout
        self._control = control
        self._workspace = workspace
        self._run_id = run_id
        self._repo = repo
        self.stage = "reading"
        self.entries = 0

    async def run(self, ticket: RunTicket) -> AgentResult:
        """Read, think (answers checked by the runner), save the checked advice."""
        if ticket.model is None:
            raise AgentFailedError("scout.not_connected")
        try:
            bundle = await self._scout.bundle(self._repo)
            profile = await self._scout._profile(self._workspace)  # the scout's own helper
            self.stage = "thinking"
            await self._control.stage(self._workspace, self._run_id, "thinking", branch=bundle.branch)
            roles = self._scout.roles
            report = await ticket.model.answer(
                scout_messages(bundle, profile, roles),
                _ScoutShape(bundle, profile, roles),
                max_tokens=SCOUT_TOKENS,
                temperature=SCOUT_TEMPERATURE,
            )
            self.stage = "checking"
            await self._control.stage(self._workspace, self._run_id, "checking")
            await self._control.finish(self._workspace, self._run_id, report.model_dump(mode="json"))
        except (SourceError, StoreError) as exc:
            raise AgentFailedError(exc.code.value) from None
        self.entries = len(report.roles)
        return AgentResult(delivered=True)

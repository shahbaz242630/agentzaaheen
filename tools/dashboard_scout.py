"""The scout on this laptop: reads a workspace's repository, asks the model, checks the answer, saves it.

Split from ``tools.dashboard`` (F17 slice 5b-2). ``LaptopScout.ask`` starts a
run (the database allows one per workspace) and returns at once; the work
runs in the background:

1. **reading**: the repository bundle (slice 5a's reader) and what the
   project is built with (slice 5b-1), from GitHub, read-only;
2. **thinking**: one answer from the model (about 5 minutes on the server's CPU);
3. **checking**: code checks the answer (``core.llm_gateway.scout``) and the
   checked advice is saved.

Runs take turns (one model answer at a time on a CPU). A failure at any stage
is saved with its reason code and logged in plain English; nothing is retried
or guessed. Stopping the dashboard cancels a run half way; the next ask closes
it as interrupted.
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
    SCOUT_TEMPERATURE,
    SCOUT_TOKENS,
    RoleBrief,
    ScoutError,
    check_answer,
    scout_messages,
)
from agentzaaheen.core.policy import Channel, Role, ScoutControl, ScoutVerdict
from agentzaaheen.core.registry.roles import RoleLibrary
from agentzaaheen.core.sdk import RepoBundle, RepoProfile, SourceError, get_logger, say
from agentzaaheen.core.store import ScoutRun, StoreError, WorkspaceRow
from agentzaaheen.plugins.connectors.github import GitHubFiles
from tools.dashboard_adding import with_github
from tools.dashboard_profile import LaptopProfiles, Repos

CRASHED = "scout.crashed"


class LaptopScout:
    """The scout's backend: starts runs and does their work in the background, one at a time."""

    def __init__(  # noqa: PLR0913  # the rules, where each repo is, GitHub, the model, the roles, more facts
        self,
        *,
        control: Callable[[], Awaitable[ScoutControl]],
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
        started, stage = time.monotonic(), "reading"
        try:
            async with self._turns:
                bundle = await with_github(
                    self._token,
                    self._transport,
                    lambda http: GitHubFiles(http, repo).bundle(now=self._clock()),
                )
                profile = await self._profile(workspace)
                stage = "thinking"
                await control.stage(workspace, run_id, "thinking", branch=bundle.branch)
                answer = await self._think(bundle, profile)
                stage = "checking"
                await control.stage(workspace, run_id, "checking")
                report = check_answer(answer, bundle, profile, self._roles)
            await control.finish(workspace, run_id, report.model_dump(mode="json"))
            say(
                self._log,
                "scout_finished",
                workspace=workspace.slug,
                duration_ms=round((time.monotonic() - started) * 1000),
                entries=len(report.roles),
            )
        except (SourceError, ScoutError, StoreError) as exc:
            code = exc.code if isinstance(exc, ScoutError) else exc.code.value
            await self._failed(control, workspace, run_id, stage, code)
        except Exception:  # noqa: BLE001  # anything else still closes the run, with a code, never a trace
            await self._failed(control, workspace, run_id, stage, CRASHED)

    async def _think(self, bundle: RepoBundle, profile: RepoProfile | None) -> str:
        if self._model is None:  # pragma: no cover  # ``connected`` is checked before any run starts
            raise ScoutError("scout.not_connected")
        messages = scout_messages(bundle, profile, self._roles)
        return await self._model.chat(messages, max_tokens=SCOUT_TOKENS, temperature=SCOUT_TEMPERATURE)

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

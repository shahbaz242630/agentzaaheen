"""Each project's product documents in the knowledge bank, on this laptop (ADR-037, F12 slice 1).

The local list ``projects/knowledge.yaml`` names, per project, the files (or
folders ending in ``/``) to read from its GitHub repository: customer-facing
documents only (READMEs, product briefs, website pages; Chairman, 29 Oct
2026). Nothing else in a repository is read. The repository is the one saved
for the workspace (``LaptopSwitches.repos_in_use``), never one the list names.

:class:`KnowledgeSync` reads the list at start and then every
``EVERY``: one call per repository for its file tree; a file is read again only
when its git blob id changed; a file no longer listed (or gone) loses its
passages. :class:`LaptopKnowledge` is the chat's search, behind the walls of
the workspace asked in.

Example ``projects/knowledge.yaml``::

    zaaheen: [README.md, site/src/pages/products.astro]
    sandoq-kin: [README.md, Vault_BRD_v1.0.md]
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import yaml
from pydantic import SecretStr

from agentzaaheen.core.sdk import SourceError, doc_pieces, get_logger, is_plain_path, say
from agentzaaheen.core.store import KnowledgeRows, Passage, StoreError, WorkspaceRow
from agentzaaheen.plugins.connectors.github import GitHubDocs
from tools.dashboard_adding import with_github
from tools.dashboard_sources import ListNotUsableError

EVERY = timedelta(hours=6)
MAX_LISTED = 40  # entries per project
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

# The repository each workspace reads, by its address.
Repos = Callable[[], Awaitable[Mapping[str, str]]]
# A workspace's passages, behind its walls (``LaptopSwitches.knowledge``).
Rows = Callable[[WorkspaceRow], Awaitable[KnowledgeRows]]


class KnowledgeListError(ValueError):
    """The local list could not be used; the message says why in plain words."""


def knowledge_list(path: Path | None) -> dict[str, tuple[str, ...]]:
    """Project address → listed entries, from the local list (none given: empty). Refuses a bad list."""
    if path is None:
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise KnowledgeListError(f"{path} could not be read ({type(exc).__name__})") from None
    if not isinstance(loaded, dict):
        raise KnowledgeListError(f"{path} must map each project to a list of files")
    listed: dict[str, tuple[str, ...]] = {}
    for slug, entries in loaded.items():
        if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
            raise KnowledgeListError(f"{path}: {slug!r} is not a project address")
        if not isinstance(entries, list) or not entries or len(entries) > MAX_LISTED:
            raise KnowledgeListError(f"{path}: {slug} needs a list of 1 to {MAX_LISTED} files")
        for entry in entries:
            if not isinstance(entry, str) or not is_plain_path(entry.rstrip("/")):
                raise KnowledgeListError(f"{path}: {slug} lists {entry!r}, which is not a plain path")
        listed[slug] = tuple(entries)
    return listed


def knowledge_settings(path: Path | None) -> dict[str, tuple[str, ...]]:
    """The list given with ``--knowledge`` (empty without one); a broken list is said and raised."""
    log = get_logger("agentzaaheen.settings")
    try:
        listed = knowledge_list(path)
    except KnowledgeListError as exc:
        print(f"dashboard: {exc}.")
        say(log, "settings_refused", setting="knowledge")
        raise ListNotUsableError from None
    if path is not None:
        say(log, "settings_loaded", setting="knowledge", entries=len(listed))
    return listed


class KnowledgeSync:
    """Keeps each listed project's documents in its knowledge: at start, then every ``EVERY``."""

    def __init__(  # noqa: PLR0913  # the list, where repositories and rows come from, GitHub, the time
        self,
        listed: Mapping[str, tuple[str, ...]],
        workspaces: Callable[[], Sequence[WorkspaceRow]],
        repos: Repos,
        rows: Rows,
        *,
        token: SecretStr,
        clock: Callable[[], datetime],
        transport: httpx.AsyncBaseTransport | None = None,
        every: timedelta = EVERY,
    ) -> None:
        self._listed = dict(listed)
        self._workspaces = workspaces
        self._repos = repos
        self._rows = rows
        self._token = token
        self._clock = clock
        self._transport = transport
        self._every_s = every.total_seconds()
        self._task: asyncio.Task[None] | None = None
        self._log = get_logger("agentzaaheen.knowledge")

    async def sync(self) -> None:
        """One round over every listed workspace; a failure in one is logged and the rest go on."""
        try:
            repos = await self._repos()
        except StoreError as exc:
            say(self._log, "knowledge_sync_failed", workspace="all", code=exc.code.value)
            return
        for workspace in self._workspaces():
            wanted = self._listed.get(workspace.slug)
            repo = repos.get(workspace.slug)
            if wanted is None or repo is None:
                continue
            try:
                await self._one(workspace, repo, wanted)
            except (SourceError, StoreError) as exc:
                say(self._log, "knowledge_sync_failed", workspace=workspace.slug, code=exc.code.value)
            except Exception as exc:  # noqa: BLE001  # one project's failure must never stop the others
                say(self._log, "knowledge_sync_crashed", workspace=workspace.slug, kind=type(exc).__name__)

    async def _one(self, workspace: WorkspaceRow, repo: str, wanted: tuple[str, ...]) -> None:
        rows = await self._rows(workspace)
        kept = await rows.known()

        async def read(http: object) -> tuple[int, int]:
            docs = GitHubDocs(http, repo)  # type: ignore[arg-type]  # a JsonClient
            found = await docs.listed(wanted)
            changed = pieces = 0
            for path, blob in found.found.items():
                if kept.get(path) == blob:
                    continue
                text = await docs.text(path, found.branch)
                cut = doc_pieces(path, text or "")
                await rows.replace(path, blob, [(p.title, p.body) for p in cut], at=self._clock())
                changed, pieces = changed + 1, pieces + len(cut)
            gone = await rows.drop([path for path in kept if path not in found.found])
            if found.missing:
                say(self._log, "knowledge_missing", workspace=workspace.slug, entries=len(found.missing))
            say(
                self._log,
                "knowledge_synced",
                workspace=workspace.slug,
                entries=len(found.found),
                changes=changed,
                count=pieces,
                removed=gone,
            )
            return changed, gone

        await with_github(self._token, self._transport, read)

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
            await self.sync()
            await asyncio.sleep(self._every_s)


class LaptopKnowledge:
    """The chat's search: the passages of the one workspace asked in that best match the question.

    Each project's first listed document is its overview: its first passage leads every answer.
    """

    def __init__(self, rows: Rows, listed: Mapping[str, tuple[str, ...]] | None = None) -> None:
        self._rows = rows
        self._overview = {
            slug: entries[0] for slug, entries in (listed or {}).items() if not entries[0].endswith("/")
        }
        self._log = get_logger("agentzaaheen.knowledge")

    async def search(self, workspace: WorkspaceRow, question: str) -> list[Passage]:
        """Up to 3 passages; none (logged) if the database can't be read, so the chat still answers."""
        try:
            rows = await self._rows(workspace)
            return await rows.search(question, overview=self._overview.get(workspace.slug))
        except StoreError as exc:
            say(self._log, "knowledge_search_failed", workspace=workspace.slug, code=exc.code.value)
            return []

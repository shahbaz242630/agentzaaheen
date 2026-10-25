"""What each workspace is built with, on this laptop: read from GitHub, kept until the repository changes.

Split from ``tools.dashboard`` (F17 slice 5b-1). Each time a workspace page
opens, ``LaptopProfiles.profile`` asks GitHub one cheap question (the default
branch and the last push time). If neither changed since the last read, the
remembered profile is served; otherwise the tree, the languages and a few
manifests are read again. So the page is up to date the moment it opens, at
the cost of one call when nothing changed.

The workspace's repository comes from the laptop database (the one saved for
that workspace when it was added) or, without a database, the fixed list in
``tools.laptop_db``. Nothing is written anywhere: the memory is this process's.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime

import httpx
from pydantic import SecretStr

from agentzaaheen.core.api.profile import Checked
from agentzaaheen.core.sdk import RepoProfile, SourceError, get_logger, say
from agentzaaheen.core.store import WorkspaceRow
from agentzaaheen.plugins.connectors.github import GitHubProfile, Head
from tools.dashboard_adding import with_github

# The repository each workspace reads, by its address (``LaptopSwitches.repos_in_use`` or the fixed list).
Repos = Callable[[], Awaitable[Mapping[str, str]]]


class LaptopProfiles:
    """The "Built with" panel's backend: one remembered profile per repository."""

    def __init__(
        self,
        token: SecretStr | None,
        repos: Repos,
        clock: Callable[[], datetime],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._repos = repos
        self._clock = clock
        self._transport = transport
        self._known: dict[str, tuple[Head, RepoProfile]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._log = get_logger("agentzaaheen.profile")

    @property
    def connected(self) -> bool:
        """True if a GitHub token was given at start."""
        return self._token is not None

    async def profile(self, workspace: WorkspaceRow) -> Checked | None:
        """The workspace's profile, read again only if its repository changed; ``None`` if it has none."""
        repo = (await self._repos()).get(workspace.slug)
        if repo is None:
            return None
        key = repo.lower()
        async with self._locks.setdefault(key, asyncio.Lock()):  # two quick opens read GitHub once
            try:
                checked, fresh = await with_github(
                    self._token, self._transport, lambda http: self._read(GitHubProfile(http, repo), key)
                )
            except SourceError as exc:
                code = exc.code.value
                say(self._log, "profile_failed", workspace=workspace.slug, code=code, source="GitHub")
                raise
        if fresh:
            say(self._log, "profile_read", workspace=workspace.slug, entries=len(checked.profile.providers))
        return checked

    async def _read(self, reader: GitHubProfile, key: str) -> tuple[Checked, bool]:
        """The profile, and whether it was read just now (``False``: remembered, nothing changed)."""
        head = await reader.head()
        now = self._clock()
        known = self._known.get(key)
        if known is not None and known[0] == head:
            return Checked(profile=known[1], checked_at=now), False
        profile = await reader.profile(head, now=now)
        self._known[key] = (head, profile)
        return Checked(profile=profile, checked_at=now), True

"""Adding a project on this laptop: the repositories the GitHub token covers, and the laptop database.

Split from ``tools.dashboard``. The Board's Add a project form lists only the
repositories the read-only token can read (``GET /user/repos``, names only),
and a repository it cannot read is refused in words before anything is
saved: add it to the token on GitHub first (ADR-039 amended 24 Oct).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
from pydantic import SecretStr

from agentzaaheen.core.api.adding import RepoOption
from agentzaaheen.core.net import JsonClient, NetPolicy
from agentzaaheen.core.policy import Added, LifecycleVerdict
from agentzaaheen.core.store import WorkspaceRow
from agentzaaheen.plugins.connectors.github import ALLOWED_HOSTS, API_HEADERS, BASE_URL, GitHubRepos
from tools.dashboard_switches import LaptopSwitches


class LaptopAdding:
    """The Add a project form's backend: the token's repositories and the laptop's workspaces."""

    def __init__(
        self,
        switches: LaptopSwitches,
        token: SecretStr | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._switches = switches
        self._token = token
        self._transport = transport

    @property
    def connected(self) -> bool:
        """True if a GitHub token was given at start."""
        return self._token is not None

    async def repos(self) -> list[RepoOption]:
        """Every repository the token covers, with the workspace that already reads it."""
        used = {repo.lower(): slug for slug, repo in (await self._switches.repos_in_use()).items()}
        return [RepoOption(repo=name, used_by=used.get(name.lower())) for name in await self._names()]

    async def add(self, name: str, repo: str | None) -> Added:
        """Add a project; a repository the token cannot read is refused before anything is saved."""
        if repo is not None:
            covered = {n.lower(): n for n in await self._names()} if self.connected else {}
            if repo.lower() not in covered:
                return Added(LifecycleVerdict.REPO_NOT_COVERED)
            repo = covered[repo.lower()]  # spelled as GitHub spells it
        return await self._switches.add_workspace(name, repo)

    async def active(self) -> list[WorkspaceRow]:
        """The workspaces the dashboard serves."""
        return await self._switches.active()

    async def _names(self) -> list[str]:
        return await with_github(self._token, self._transport, lambda http: GitHubRepos(http).names())


async def with_github[T](
    token: SecretStr | None,
    transport: httpx.AsyncBaseTransport | None,
    use: Callable[[JsonClient], Awaitable[T]],
) -> T:
    """Run ``use`` with a read-only GitHub client that carries the token (never logged)."""
    if token is None:  # pragma: no cover  # callers check ``connected`` first
        raise RuntimeError("no GitHub token was given")
    async with JsonClient(
        BASE_URL,
        policy=NetPolicy(allowed_hosts=ALLOWED_HOSTS),
        headers=API_HEADERS,
        secret_headers={"Authorization": SecretStr(f"Bearer {token.get_secret_value()}")},
        transport=transport,
    ) as http:
        return await use(http)

"""An in-memory GitHub source for tests of anything that consumes code health.

It passes the same contract suite as the real ``GitHubSource``
(``tests/contract/test_repo_source_contract.py``), so code tested against it
behaves the same against GitHub.
"""

from __future__ import annotations

from datetime import datetime

from yaqzan.core.sdk import RepoHealth, SourceError, SourceErrorCode
from yaqzan.plugins.connectors.github.mapping import SOURCE


class FakeGitHubSource:
    """Serves one fixed ``RepoHealth`` (stamped with the fetch time). Set ``fail_with`` to fail instead."""

    def __init__(self, health: RepoHealth) -> None:
        self.health = health
        self.fail_with: SourceErrorCode | None = None
        self.calls = 0

    @property
    def source(self) -> str:
        """The connector id."""
        return SOURCE

    @property
    def repo(self) -> str:
        """The repository this fake stands in for."""
        return self.health.repo

    async def fetch(self, *, now: datetime) -> RepoHealth:
        """The fixed health as of ``now``."""
        self.calls += 1
        if self.fail_with is not None:
            raise SourceError(self.fail_with, "the fake was told to fail")
        return self.health.model_copy(update={"fetched_at": now})

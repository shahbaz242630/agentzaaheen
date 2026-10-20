"""The code-health shape: one repository's open work and whether its main branch builds (METRICS §6).

Every code-hosting connector (GitHub now, others later) maps its vendor data
to :class:`RepoHealth`. It holds counts, a commit id and times only: never an
issue title, a pull request body or a commit message. Those are written by
anyone, so they are untrusted text that must never reach a model as a fact.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

_SOURCE = r"^[a-z][a-z0-9_]{0,63}$"
# owner/name as code hosts allow them (GitHub: owner up to 39, name up to 100 characters).
REPO_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}$"
_BRANCH = r"^[A-Za-z0-9._/-]{1,255}$"
_COMMIT = r"^[0-9a-f]{40}$"


class _Shape(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BuildState(StrEnum):
    """How the main branch builds: each workflow's latest run there, taken together."""

    PASSED = "passed"
    FAILED = "failed"
    RUNNING = "running"
    INCONCLUSIVE = "inconclusive"  # every latest run was cancelled or skipped: nothing passed
    NONE = "none"  # no build has ever run on the main branch


class RepoHealth(_Shape):
    """One repository right now. ``failing_workflows`` counts workflows whose latest run on main failed.

    ``build_commit`` is the commit of the newest of those runs.
    """

    source: str = Field(pattern=_SOURCE)
    repo: str = Field(pattern=REPO_PATTERN)
    default_branch: str = Field(pattern=_BRANCH)
    open_issues: int = Field(ge=0)
    open_pull_requests: int = Field(ge=0)
    build: BuildState
    build_commit: str | None = Field(default=None, pattern=_COMMIT)
    failing_workflows: int = Field(default=0, ge=0)
    build_finished_at: AwareDatetime | None = None
    fetched_at: AwareDatetime

    @model_validator(mode="after")
    def _build_adds_up(self) -> RepoHealth:
        if self.build is BuildState.NONE:
            if self.build_commit is not None or self.build_finished_at is not None:
                raise ValueError("a repository with no build has no build commit or time")
        elif self.build_commit is None:
            raise ValueError(f"a {self.build} build names the commit it built")
        if (self.build is BuildState.FAILED) != (self.failing_workflows > 0):
            raise ValueError("a failed build has failing workflows, and only a failed build does")
        finished = {BuildState.PASSED, BuildState.FAILED, BuildState.INCONCLUSIVE}
        if (self.build in finished) != (self.build_finished_at is not None):
            raise ValueError("a finished build has a finish time, and only a finished build does")
        return self


class RepoSource(Protocol):
    """What every code-hosting connector offers: one read-only look at one repository."""

    @property
    def source(self) -> str:
        """The connector id, e.g. ``github``."""
        ...

    @property
    def repo(self) -> str:
        """The repository it reads, ``owner/name``."""
        ...

    async def fetch(self, *, now: datetime) -> RepoHealth:
        """The repository as it is now. Raises :class:`~yaqzan.core.sdk.SourceError` only."""
        ...

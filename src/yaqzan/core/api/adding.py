"""Adding a project from the Board (F17 slice 5a, ADR-039 amended 24 Oct).

``GET /api/github/repos`` lists the repositories the read-only GitHub token
covers, each marked if a workspace already reads it, so the Board offers only
repositories Yaqzan can read. ``POST /api/workspaces`` adds a workspace from a
name and (optionally) one of those repositories. No Windows Hello: a new
workspace has no agents, is switched off and has no cap. After adding, the
workspace directory is read again, so the new address answers at once.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from yaqzan.core.api.workspaces import SLUG, WorkspaceDirectory
from yaqzan.core.policy import Added, LifecycleVerdict
from yaqzan.core.sdk import REPO_PATTERN, SourceError
from yaqzan.core.store import StoreError, WorkspaceRow


class _View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RepoOption(_View):
    """One repository the token covers. ``used_by``: the workspace that reads it already, if any."""

    repo: str = Field(pattern=REPO_PATTERN)
    used_by: str | None = Field(default=None, pattern=SLUG)


class RepoList(_View):
    """What the Add a project form offers. ``connected``: a GitHub token was given at start."""

    repos: tuple[RepoOption, ...]
    connected: bool


class AddIn(_View):
    """A new project: its name, and the repository it reads (or none)."""

    name: str = Field(max_length=200)
    repo: str | None = Field(default=None, max_length=142)


class AddedOut(_View):
    """The new workspace's address and name."""

    slug: str = Field(pattern=SLUG)
    name: str = Field(max_length=80)


class WorkspaceAdding(Protocol):
    """What the adding routes need (``LaptopAdding`` in real use)."""

    @property
    def connected(self) -> bool:
        """True if a GitHub token was given, so repositories can be listed and read."""
        ...

    async def repos(self) -> list[RepoOption]:
        """The repositories the token covers. Raises ``SourceError`` if GitHub cannot be read."""
        ...

    async def add(self, name: str, repo: str | None) -> Added:
        """Add a workspace."""
        ...

    async def active(self) -> list[WorkspaceRow]:
        """The workspaces the dashboard serves."""
        ...


def adding_routes(
    app: FastAPI,
    adding: WorkspaceAdding | None,
    directory: WorkspaceDirectory,
    refuse_foreign: Callable[..., Any],
) -> None:
    """The routes for adding a project."""

    def ready() -> WorkspaceAdding:
        if adding is None:
            raise HTTPException(status_code=503, detail="workspaces.read_only")
        return adding

    @app.get("/api/github/repos")
    async def list_repos() -> RepoList:
        if adding is None or not adding.connected:
            return RepoList(repos=(), connected=False)
        try:
            return RepoList(repos=tuple(await adding.repos()), connected=True)
        except SourceError as exc:
            raise HTTPException(status_code=503, detail=exc.code.value) from None
        except StoreError:
            raise HTTPException(status_code=503, detail="workspaces.store_unavailable") from None

    @app.post("/api/workspaces", status_code=201, dependencies=[Depends(refuse_foreign)])
    async def add(wanted: AddIn) -> AddedOut:
        rules = ready()
        try:
            added = await rules.add(wanted.name, wanted.repo)
            if added.verdict is not LifecycleVerdict.DONE or added.workspace is None:
                raise HTTPException(status_code=409, detail=added.verdict.value)
            directory.replace(await rules.active())
        except SourceError as exc:
            raise HTTPException(status_code=503, detail=exc.code.value) from None
        except StoreError:
            raise HTTPException(status_code=503, detail="workspaces.store_unavailable") from None
        return AddedOut(slug=added.workspace.slug, name=added.workspace.name)

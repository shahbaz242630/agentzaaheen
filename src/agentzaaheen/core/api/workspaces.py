"""The workspace door: the dashboard picks a workspace once, by its address, and nowhere else (ADR-039).

Every per-project route is ``/api/workspaces/{slug}/...`` and takes its
workspace through :data:`InWorkspace`. The door looks the slug up in the
server's own list (:class:`WorkspaceDirectory`, from the database on the
laptop, never from the browser) and hands the route that one workspace.
Nothing further in takes a workspace from a request body, so a wrong id in the
middle of the code has nowhere to go. An unknown slug is refused.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from fastapi import Depends, HTTPException, Path, Request

from agentzaaheen.core.store import WorkspaceRow

SLUG = r"^[a-z0-9]+(-[a-z0-9]+)*$"
# Where the door leaves the chosen workspace's slug for the request's log line.
SCOPE_KEY = "agentzaaheen.workspace"


class WorkspaceDirectory:
    """The tenant's active workspaces this dashboard serves, read by the server from the database.

    Read at start, and again whenever a workspace is archived or brought back,
    so an archived workspace's address stops answering at once.
    """

    def __init__(self, workspaces: Sequence[WorkspaceRow]) -> None:
        self._by_slug = _by_slug(workspaces)

    def replace(self, workspaces: Sequence[WorkspaceRow]) -> None:
        """Serve this list from now on (read fresh from the database)."""
        self._by_slug = _by_slug(workspaces)

    def all(self) -> tuple[WorkspaceRow, ...]:
        """Every workspace, in the order given."""
        return tuple(self._by_slug.values())

    def find(self, slug: str) -> WorkspaceRow | None:
        """One workspace by its address, or ``None``."""
        return self._by_slug.get(slug)


def _by_slug(workspaces: Sequence[WorkspaceRow]) -> dict[str, WorkspaceRow]:
    found = {w.slug: w for w in workspaces}
    if len(found) != len(workspaces):
        raise ValueError("workspace slugs must be unique")
    return found


def directory_of(request: Request) -> WorkspaceDirectory:
    """The directory the app was built with."""
    found = request.app.state.workspaces
    if not isinstance(found, WorkspaceDirectory):  # pragma: no cover  # create_app always sets it
        raise TypeError("the app has no workspace directory")
    return found


def door(request: Request, slug: Annotated[str, Path(pattern=SLUG, max_length=64)]) -> WorkspaceRow:
    """The workspace in the address, or 404 ``workspace.not_found``."""
    found = directory_of(request).find(slug)
    if found is None:
        raise HTTPException(status_code=404, detail="workspace.not_found")
    request.scope[SCOPE_KEY] = found.slug
    return found


InWorkspace = Annotated[WorkspaceRow, Depends(door)]

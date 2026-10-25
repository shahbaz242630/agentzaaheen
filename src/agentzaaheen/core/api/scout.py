"""Asking the scout about a workspace, and reading its advice (F17 slice 5b-2, ADR-039).

``POST /api/workspaces/{slug}/scout`` starts a run in the background and
answers at once (202) with the run's state; ``GET`` on the same address shows
the newest run: where it is (reading, thinking, checking) while it runs, then
the advice code has checked, or why it failed. The workspace comes from the
address (the door), never from the body. No Windows Hello: the scout only
reads and advises; nothing is switched on and nothing spends because of it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol

from fastapi import Depends, FastAPI, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from agentzaaheen.core.api.workspaces import InWorkspace
from agentzaaheen.core.llm_gateway.scout import ScoutReport
from agentzaaheen.core.store import ScoutRun, StoreError, WorkspaceRow

Asked = Literal["scout.started", "scout.busy", "scout.not_allowed", "scout.no_repo"]
_REFUSED_STATUS = {"scout.busy": 409, "scout.no_repo": 409, "scout.not_allowed": 403}


class _View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ScoutView(_View):
    """The workspace's newest scout run, as the page shows it. ``connected``: the scout can be asked here."""

    connected: bool
    state: Literal["none", "running", "done", "failed"]
    stage: Literal["reading", "thinking", "checking", "finished"] | None = None
    repo: str | None = None
    branch: str | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    reason: str | None = None
    report: ScoutReport | None = None


class WorkspaceScout(Protocol):
    """What the scout routes need (``LaptopScout`` in real use)."""

    @property
    def connected(self) -> bool:
        """True if the scout can run here: a database, a GitHub token and a model."""
        ...

    async def ask(self, workspace: WorkspaceRow) -> Asked:
        """Start a run in the background; say whether it started."""
        ...

    async def latest(self, workspace: WorkspaceRow) -> ScoutRun | None:
        """The workspace's newest run."""
        ...


def scout_view(run: ScoutRun | None, *, connected: bool) -> ScoutView:
    """The page's view of one run. Saved advice that no longer reads as a report shows as failed."""
    if run is None:
        return ScoutView(connected=connected, state="none")
    fields: dict[str, Any] = {
        "connected": connected,
        "state": run.state,
        "stage": run.stage,
        "repo": run.repo,
        "branch": run.branch,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "reason": run.reason,
    }
    if run.result is not None:
        try:
            fields["report"] = ScoutReport.model_validate(run.result)
        except ValidationError:
            fields |= {"state": "failed", "reason": "scout.unreadable"}
    return ScoutView(**fields)


def scout_routes(app: FastAPI, scout: WorkspaceScout | None, refuse_foreign: Callable[..., Any]) -> None:
    """The routes for asking the scout and reading its advice."""

    async def view(workspace: WorkspaceRow) -> ScoutView:
        if scout is None:
            return ScoutView(connected=False, state="none")
        try:
            return scout_view(await scout.latest(workspace), connected=scout.connected)
        except StoreError:
            raise HTTPException(status_code=503, detail="scout.store_unavailable") from None

    @app.get("/api/workspaces/{slug}/scout")
    async def get_scout(workspace: InWorkspace) -> ScoutView:
        return await view(workspace)

    @app.post("/api/workspaces/{slug}/scout", status_code=202, dependencies=[Depends(refuse_foreign)])
    async def ask_scout(workspace: InWorkspace) -> ScoutView:
        if scout is None or not scout.connected:
            raise HTTPException(status_code=503, detail="scout.not_connected")
        try:
            asked = await scout.ask(workspace)
        except StoreError:
            raise HTTPException(status_code=503, detail="scout.store_unavailable") from None
        if asked != "scout.started":
            raise HTTPException(status_code=_REFUSED_STATUS[asked], detail=asked)
        return await view(workspace)

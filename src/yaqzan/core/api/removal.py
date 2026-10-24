"""Removing a project from the dashboard: archive, bring back, delete for good (F17 slice 4, ADR-039).

Archiving goes through the workspace door (``/api/workspaces/{slug}/archive``):
only an active workspace has an address there. An archived workspace has no
address behind the door any more, so bringing it back or deleting it uses
``/api/archived/{slug}/...``, looked up in the database's own list of archived
workspaces, never from the browser.

Each change needs Windows Hello to sign that exact change (``workspaces.needs_passkey``,
then ``.../options``, then the change again with the signed answer). Deleting
for good is only possible once archived, so it always takes two signatures.
After a change the workspace directory is read again, so the door at once
stops (or starts) answering for that workspace.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol

from fastapi import Depends, FastAPI, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from yaqzan.core.api.agents import StepUpOptions
from yaqzan.core.api.workspaces import SCOPE_KEY, SLUG, InWorkspace, WorkspaceDirectory
from yaqzan.core.auth import PasskeyError
from yaqzan.core.policy import Channel, LifecycleVerdict, PasskeyApprovals, Removal, Role, WorkspaceControl
from yaqzan.core.store import StoreError, WorkspaceRow

Step = Literal["archive", "restore", "erase"]


class _View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SignedIn(_View):
    """Windows Hello's signed answer, once the change has asked for one."""

    credential: dict[str, Any] | None = None


class ArchivedOut(_View):
    """What archiving switched off and deleted."""

    agents_stopped: int = Field(ge=0)
    keys_deleted: int = Field(ge=0)


class ArchivedWorkspace(_View):
    """One archived workspace, as the Board lists it."""

    slug: str = Field(pattern=SLUG, max_length=64)
    name: str = Field(max_length=80)


class ArchivedList(_View):
    """The archived workspaces. ``switches``: changes are possible here (there is a database)."""

    workspaces: tuple[ArchivedWorkspace, ...]
    switches: bool


def purpose(step: Step, workspace: WorkspaceRow) -> str:
    """The challenge's purpose: names exactly this change to this workspace."""
    return f"step:workspace:{workspace.workspace_id}:{step}"


class WorkspaceRemoval(Protocol):
    """What the removal routes need (``DashboardRemoval`` in real use)."""

    async def active(self) -> list[WorkspaceRow]:
        """The workspaces the dashboard serves."""
        ...

    async def archived(self) -> list[WorkspaceRow]:
        """The archived workspaces."""
        ...

    async def archive(self, workspace: WorkspaceRow, credential: dict[str, Any] | None) -> Removal:
        """Archive one workspace."""
        ...

    async def restore(self, workspace: WorkspaceRow, credential: dict[str, Any] | None) -> LifecycleVerdict:
        """Bring one archived workspace back."""
        ...

    async def erase(self, workspace: WorkspaceRow, credential: dict[str, Any] | None) -> LifecycleVerdict:
        """Delete one archived workspace for good."""
        ...

    async def options(self, step: str) -> dict[str, Any]:
        """Options for Windows Hello to sign the change named by ``step``."""
        ...


class DashboardRemoval:
    """Removing projects on this laptop: the dashboard acts as the Chairman (no login yet)."""

    def __init__(self, control: WorkspaceControl, passkeys: PasskeyApprovals) -> None:
        self._control = control
        self._passkeys = passkeys

    async def active(self) -> list[WorkspaceRow]:
        """The workspaces the dashboard serves."""
        return await self._control.active()

    async def archived(self) -> list[WorkspaceRow]:
        """The archived workspaces."""
        return await self._control.archived()

    async def archive(self, workspace: WorkspaceRow, credential: dict[str, Any] | None) -> Removal:
        """Archive; a signed answer must be for exactly this change."""
        step = await self._signed("archive", workspace, credential)
        if step is False:
            return Removal(LifecycleVerdict.PASSKEY_FAILED)
        return await self._control.archive(
            workspace.workspace_id, role=Role.CHAIRMAN, channel=Channel.DASHBOARD, step_up_at=step
        )

    async def restore(self, workspace: WorkspaceRow, credential: dict[str, Any] | None) -> LifecycleVerdict:
        """Bring back; a signed answer must be for exactly this change."""
        step = await self._signed("restore", workspace, credential)
        if step is False:
            return LifecycleVerdict.PASSKEY_FAILED
        return await self._control.restore(
            workspace.workspace_id, role=Role.CHAIRMAN, channel=Channel.DASHBOARD, step_up_at=step
        )

    async def erase(self, workspace: WorkspaceRow, credential: dict[str, Any] | None) -> LifecycleVerdict:
        """Delete for good; a signed answer must be for exactly this change."""
        step = await self._signed("erase", workspace, credential)
        if step is False:
            return LifecycleVerdict.PASSKEY_FAILED
        return await self._control.erase(
            workspace.workspace_id, role=Role.CHAIRMAN, channel=Channel.DASHBOARD, step_up_at=step
        )

    async def options(self, step: str) -> dict[str, Any]:
        """Options for Windows Hello to sign the change named by ``step``."""
        return await self._passkeys.step_up_options(step)

    async def _signed(
        self, step: Step, workspace: WorkspaceRow, credential: dict[str, Any] | None
    ) -> datetime | Literal[False] | None:
        """``None`` when unsigned (the control then asks for it), the step-up time, or False if it failed."""
        if credential is None:
            return None
        signed = await self._passkeys.step_up(purpose(step, workspace), credential)
        return False if signed is None else signed


async def archived_door(
    request: Request, slug: Annotated[str, Path(pattern=SLUG, max_length=64)]
) -> WorkspaceRow:
    """The archived workspace in the address (from the database's list), or 404 ``not_archived``."""
    removal: WorkspaceRemoval | None = request.app.state.removal
    if removal is None:
        raise HTTPException(status_code=503, detail="workspaces.read_only")
    try:
        archived = await removal.archived()
    except StoreError:
        raise HTTPException(status_code=503, detail="workspaces.store_unavailable") from None
    for workspace in archived:
        if workspace.slug == slug:
            request.scope[SCOPE_KEY] = workspace.slug
            return workspace
    raise HTTPException(status_code=404, detail="workspace.not_archived")


InArchive = Annotated[WorkspaceRow, Depends(archived_door)]


def removal_routes(
    app: FastAPI,
    removal: WorkspaceRemoval | None,
    directory: WorkspaceDirectory,
    refuse_foreign: Callable[..., Any],
) -> None:
    """The routes for removing a project, and for the archived ones."""
    app.state.removal = removal

    def ready() -> WorkspaceRemoval:
        if removal is None:
            raise HTTPException(status_code=503, detail="workspaces.read_only")
        return removal

    async def stored[T](call: Awaitable[T]) -> T:
        try:
            return await call
        except StoreError:
            raise HTTPException(status_code=503, detail="workspaces.store_unavailable") from None
        except PasskeyError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from None

    async def done(verdict: LifecycleVerdict) -> None:
        if verdict is not LifecycleVerdict.DONE:
            raise HTTPException(status_code=409, detail=verdict.value)
        directory.replace(await stored(ready().active()))

    @app.get("/api/archived")
    async def list_archived() -> ArchivedList:
        if removal is None:
            return ArchivedList(workspaces=(), switches=False)
        found = await stored(removal.archived())
        return ArchivedList(
            workspaces=tuple(ArchivedWorkspace(slug=w.slug, name=w.name) for w in found), switches=True
        )

    @app.post("/api/workspaces/{slug}/archive", dependencies=[Depends(refuse_foreign)])
    async def archive(workspace: InWorkspace, signed: SignedIn) -> ArchivedOut:
        removed = await stored(ready().archive(workspace, signed.credential))
        await done(removed.verdict)
        gone = removed.archived
        return ArchivedOut(
            agents_stopped=gone.agents_stopped if gone else 0, keys_deleted=gone.keys_deleted if gone else 0
        )

    @app.post("/api/workspaces/{slug}/archive/options", dependencies=[Depends(refuse_foreign)])
    async def archive_options(workspace: InWorkspace) -> StepUpOptions:
        return StepUpOptions(options=await stored(ready().options(purpose("archive", workspace))))

    @app.post("/api/archived/{slug}/restore", dependencies=[Depends(refuse_foreign)])
    async def restore(workspace: InArchive, signed: SignedIn) -> ArchivedList:
        await done(await stored(ready().restore(workspace, signed.credential)))
        return await list_archived()

    @app.post("/api/archived/{slug}/restore/options", dependencies=[Depends(refuse_foreign)])
    async def restore_options(workspace: InArchive) -> StepUpOptions:
        return StepUpOptions(options=await stored(ready().options(purpose("restore", workspace))))

    @app.post("/api/archived/{slug}/erase", dependencies=[Depends(refuse_foreign)])
    async def erase(workspace: InArchive, signed: SignedIn) -> ArchivedList:
        await done(await stored(ready().erase(workspace, signed.credential)))
        return await list_archived()

    @app.post("/api/archived/{slug}/erase/options", dependencies=[Depends(refuse_foreign)])
    async def erase_options(workspace: InArchive) -> StepUpOptions:
        return StepUpOptions(options=await stored(ready().options(purpose("erase", workspace))))

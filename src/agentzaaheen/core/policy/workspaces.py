"""Adding a project (F17 slice 5a); removing one: archive, bring back, delete for good (slice 4).

Adding needs the Chairman but no step-up (ADR-039 amended 24 Oct): a new
workspace has no agents, is switched off and has no cap, so nothing in it
can run or spend until a passkey says so. Its address comes from its name
and is never reused (archived and deleted workspaces keep theirs), and a
GitHub repository belongs to at most one workspace.

The other three need the Chairman on the dashboard with a fresh passkey step-up:
archiving deletes the saved keys, which cannot be undone; bringing back opens
the workspace to new rows again; deleting erases its history. The database
keeps the same rules underneath (migration 0008), and only an archived
workspace can be deleted, so deleting always takes two separate step-ups.
Every decision is audited; the audit log itself is never deleted.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from agentzaaheen.core.policy.access import Channel, Operation, Role, authorize
from agentzaaheen.core.policy.audit import AuditDraft, AuditOutcome
from agentzaaheen.core.policy.ledger import Ledger
from agentzaaheen.core.sdk import REPO_PATTERN, get_logger, say
from agentzaaheen.core.store import (
    Archived,
    Database,
    StateChange,
    StoreError,
    StoreErrorCode,
    WorkspaceLifecycle,
    WorkspaceRow,
    WorkspaceRows,
    WorkspaceSources,
)

MAX_NAME = 80
MAX_SLUG = 40

_ACTORS = {
    Role.CHAIRMAN: "user:chairman",
    Role.VIEWER: "user:viewer",
    Role.AI_OPERATOR: "system:ai-operator",
    Role.AGENT_AUDITOR: "system:agent-auditor",
}


class LifecycleVerdict(StrEnum):
    """What happened to one attempt. Safe to log, audit and show."""

    DONE = "workspaces.done"
    NOT_ALLOWED = "workspaces.not_allowed"
    NEEDS_PASSKEY = "workspaces.needs_passkey"  # sign this exact change with Windows Hello
    PASSKEY_FAILED = "workspaces.passkey_failed"
    NOT_ACTIVE = "workspaces.not_active"  # only an active workspace can be archived
    NOT_ARCHIVED = "workspaces.not_archived"  # only an archived one can be brought back or deleted
    BAD_NAME = "workspaces.bad_name"  # empty, too long, or nothing usable for an address
    BAD_REPO = "workspaces.bad_repo"  # not written owner/name
    NAME_TAKEN = "workspaces.name_taken"  # its address is used (or was, by an archived or deleted one)
    REPO_TAKEN = "workspaces.repo_taken"  # another workspace of this tenant reads that repository
    REPO_NOT_COVERED = "workspaces.repo_not_covered"  # the read-only token cannot read it (yet)


@dataclass(frozen=True)
class Added:
    """The verdict, and the new workspace when it was added."""

    verdict: LifecycleVerdict
    workspace: WorkspaceRow | None = None


def slug_of(name: str) -> str:
    """The address made from a name: lower case, words joined by ``-``, at most 40 characters."""
    words = re.findall(r"[a-z0-9]+", name.lower())
    slug = ""
    for word in words:
        joined = f"{slug}-{word}" if slug else word
        if len(joined) > MAX_SLUG:
            break
        slug = joined
    return slug or (words[0][:MAX_SLUG] if words else "")


@dataclass(frozen=True)
class Removal:
    """The verdict, and for an archive what it switched off and deleted."""

    verdict: LifecycleVerdict
    archived: Archived | None = None


class WorkspaceControl:
    """The rules for archiving, bringing back and deleting a tenant's workspaces."""

    def __init__(self, db: Database, tenant_id: UUID, clock: Callable[[], datetime]) -> None:
        self._db = db
        self._tenant = tenant_id
        self._clock = clock
        self._ledger = Ledger(db, tenant_id)

    def _lifecycle(self, workspace_id: UUID) -> WorkspaceLifecycle:
        # The workspace was chosen at a door: the API's address, checked against the database's own list.
        return WorkspaceLifecycle(self._db.scoped(self._tenant, workspace_id))

    async def active(self) -> list[WorkspaceRow]:
        """The workspaces the dashboard serves."""
        return await WorkspaceRows(self._db.scoped(self._tenant)).workspaces()

    async def archived(self) -> list[WorkspaceRow]:
        """The archived workspaces, which can be brought back or deleted for good."""
        return await WorkspaceRows(self._db.scoped(self._tenant)).archived()

    async def add(self, name: str, *, github: str | None, role: Role, channel: Channel) -> Added:
        """Add a workspace named ``name``, reading ``github`` (``owner/name``) if given."""
        now = self._clock()
        clean = " ".join(name.split())
        new_id = uuid4()
        # A name with no Latin letters or digits (an Arabic name) still gets an address.
        slug = slug_of(clean) or (f"project-{new_id.hex[:8]}" if clean else "")
        added = None
        if not _may(Operation.ADD_WORKSPACE, role, channel, None, now):
            verdict = LifecycleVerdict.NOT_ALLOWED
        elif not slug or len(clean) > MAX_NAME or not clean.isprintable():
            verdict = LifecycleVerdict.BAD_NAME
        elif github is not None and not _is_repo(github):
            verdict = LifecycleVerdict.BAD_REPO
        elif slug in await WorkspaceRows(self._db.scoped(self._tenant)).slugs():
            verdict = LifecycleVerdict.NAME_TAKEN
        elif github is not None and github.lower() in {r.lower() for r in (await self.repos()).values()}:
            verdict = LifecycleVerdict.REPO_TAKEN
        else:
            added = WorkspaceRow(workspace_id=new_id, slug=slug, name=clean)
            verdict = await self._create(added, github, _ACTORS[role], now)
        done = added if verdict is LifecycleVerdict.DONE else None
        details: dict[str, Any] = {"verdict": verdict.value, "slug": slug, "repo": github}
        await self._audit(
            "workspaces.add", verdict, role, done.workspace_id if done else None, now, details=details
        )
        return Added(verdict, done)

    async def _create(
        self, workspace: WorkspaceRow, github: str | None, by: str, now: datetime
    ) -> LifecycleVerdict:
        # The new workspace's id is made here, so this is the door for its own handle.
        try:
            await WorkspaceRows(self._db.scoped(self._tenant, workspace.workspace_id)).create(
                workspace, github=github, by=by, at=now
            )
        except StoreError as exc:
            if exc.code is not StoreErrorCode.CONFLICT:
                raise
            # Taken in between (the database's own unique rules): say which, as far as we can see.
            taken = await WorkspaceRows(self._db.scoped(self._tenant)).slugs()
            return LifecycleVerdict.NAME_TAKEN if workspace.slug in taken else LifecycleVerdict.REPO_TAKEN
        return LifecycleVerdict.DONE

    async def repos(self) -> dict[str, str]:
        """The GitHub repository each active or archived workspace reads, by its address."""
        found: dict[str, str] = {}
        for workspace in [*await self.active(), *await self.archived()]:
            repo = await WorkspaceSources(self._db.scoped(self._tenant, workspace.workspace_id)).github()
            if repo is not None:
                found[workspace.slug] = repo
        return found

    async def archive(
        self, workspace_id: UUID, *, role: Role, channel: Channel, step_up_at: datetime | None
    ) -> Removal:
        """Switch everything off, delete the saved keys, and keep the history."""
        now = self._clock()
        archived = None
        if step_up_at is None:
            verdict = LifecycleVerdict.NEEDS_PASSKEY
        elif not _may(Operation.ARCHIVE_WORKSPACE, role, channel, step_up_at, now):
            verdict = LifecycleVerdict.NOT_ALLOWED
        else:
            archived = await self._lifecycle(workspace_id).archive(
                StateChange(_ACTORS[role], now, step_up_at)
            )
            verdict = LifecycleVerdict.DONE if archived else LifecycleVerdict.NOT_ACTIVE
        details: dict[str, Any] = {"verdict": verdict.value}
        if archived is not None:
            details |= {"agents_stopped": archived.agents_stopped, "keys_deleted": archived.keys_deleted}
        await self._audit("workspaces.archive", verdict, role, workspace_id, now, details=details)
        return Removal(verdict, archived)

    async def restore(
        self, workspace_id: UUID, *, role: Role, channel: Channel, step_up_at: datetime | None
    ) -> LifecycleVerdict:
        """Bring an archived workspace back. Its agents stay off and its keys must be added again."""
        now = self._clock()
        if step_up_at is None:
            verdict = LifecycleVerdict.NEEDS_PASSKEY
        elif not _may(Operation.RESTORE_WORKSPACE, role, channel, step_up_at, now):
            verdict = LifecycleVerdict.NOT_ALLOWED
        else:
            restored = await self._lifecycle(workspace_id).restore(
                StateChange(_ACTORS[role], now, step_up_at)
            )
            verdict = LifecycleVerdict.DONE if restored else LifecycleVerdict.NOT_ARCHIVED
        await self._audit(
            "workspaces.restore", verdict, role, workspace_id, now, details={"verdict": verdict.value}
        )
        return verdict

    async def erase(
        self, workspace_id: UUID, *, role: Role, channel: Channel, step_up_at: datetime | None
    ) -> LifecycleVerdict:
        """Delete an archived workspace's data for good. The audit log keeps the record."""
        now = self._clock()
        if step_up_at is None:
            verdict = LifecycleVerdict.NEEDS_PASSKEY
        elif not _may(Operation.ERASE_WORKSPACE, role, channel, step_up_at, now):
            verdict = LifecycleVerdict.NOT_ALLOWED
        elif workspace_id not in {w.workspace_id for w in await self.archived()}:
            verdict = LifecycleVerdict.NOT_ARCHIVED
        else:
            await self._lifecycle(workspace_id).erase(StateChange(_ACTORS[role], now, step_up_at))
            verdict = LifecycleVerdict.DONE
        await self._audit(
            "workspaces.erase", verdict, role, workspace_id, now, details={"verdict": verdict.value}
        )
        return verdict

    async def _audit(  # noqa: PLR0913  # each is a separate audit field
        self,
        action: str,
        verdict: LifecycleVerdict,
        role: Role,
        workspace_id: UUID | None,
        now: datetime,
        *,
        details: dict[str, Any],
    ) -> None:
        say(
            get_logger("agentzaaheen.workspaces"),
            "workspace_lifecycle_decided",
            action=action,
            actor=_ACTORS[role],
            verdict=verdict.value,
        )
        await self._ledger.record(
            AuditDraft(
                at=now,
                actor=_ACTORS[role],
                action=action,
                outcome=AuditOutcome.ALLOW if verdict is LifecycleVerdict.DONE else AuditOutcome.DENY,
                reason_code=verdict.value,
                correlation_id=uuid4(),
                project_id=workspace_id,
                target_ref=f"workspace:{workspace_id or details.get('slug')}",
                details=details,
            )
        )


def _is_repo(repo: str) -> bool:
    return bool(re.fullmatch(REPO_PATTERN, repo)) and repo.partition("/")[2] not in {".", ".."}


def _may(
    operation: Operation, role: Role, channel: Channel, step_up_at: datetime | None, now: datetime
) -> bool:
    return authorize(role, operation, channel, step_up_at=step_up_at, now=now).allowed

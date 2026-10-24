"""Removing a project: archive it, bring it back, or delete it for good (F17 slice 4, ADR-039).

All three need the Chairman on the dashboard with a fresh passkey step-up:
archiving deletes the saved keys, which cannot be undone; bringing back opens
the workspace to new rows again; deleting erases its history. The database
keeps the same rules underneath (migration 0008), and only an archived
workspace can be deleted, so deleting always takes two separate step-ups.
Every decision is audited; the audit log itself is never deleted.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from yaqzan.core.policy.access import Channel, Operation, Role, authorize
from yaqzan.core.policy.audit import AuditDraft, AuditOutcome
from yaqzan.core.policy.ledger import Ledger
from yaqzan.core.sdk import get_logger, say
from yaqzan.core.store import (
    Archived,
    Database,
    StateChange,
    WorkspaceLifecycle,
    WorkspaceRow,
    WorkspaceRows,
)

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
        workspace_id: UUID,
        now: datetime,
        *,
        details: dict[str, Any],
    ) -> None:
        say(
            get_logger("yaqzan.workspaces"),
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
                target_ref=f"workspace:{workspace_id}",
                details=details,
            )
        )


def _may(operation: Operation, role: Role, channel: Channel, step_up_at: datetime, now: datetime) -> bool:
    return authorize(role, operation, channel, step_up_at=step_up_at, now=now).allowed

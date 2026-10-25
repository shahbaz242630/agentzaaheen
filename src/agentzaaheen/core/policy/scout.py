"""Asking the scout about one workspace, and keeping its runs (F17 slice 5b-2, ADR-039).

Starting the scout is a read: it reads the workspace's repository and writes
advice, and nothing is switched on or spends because of it. So the Chairman
may start it from the dashboard without Windows Hello (``ASK_SCOUT`` reduces
no safety and adds no risk). Each start, refused or not, is written to the
audit log first-hand (``workspaces.scout``).

This is the door for the scout's runs: the workspace comes from the API
door's address (the dashboard page the Chairman opened), never from a request
body, and each run is saved behind that workspace's wall. One run at a time
per workspace (the database's own rule); a run left half way by a dashboard
that stopped is closed as interrupted after :data:`STALE_AFTER`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from agentzaaheen.core.policy.access import Channel, Operation, Role, authorize
from agentzaaheen.core.policy.audit import AuditDraft, AuditOutcome
from agentzaaheen.core.policy.ledger import Ledger
from agentzaaheen.core.sdk import get_logger, say
from agentzaaheen.core.store import Database, ScoutRun, ScoutRuns, WorkspaceRow
from agentzaaheen.core.store.scout import Stage

STALE_AFTER = timedelta(minutes=30)  # a CPU scout run takes about 5 minutes; 30 means it stopped
_ACTOR = {Role.CHAIRMAN: "user:chairman", Role.VIEWER: "user:viewer", Role.AI_OPERATOR: "system:ai-operator",
          Role.AGENT_AUDITOR: "system:agent-auditor"}  # fmt: skip


class ScoutVerdict(StrEnum):
    """What happened to one ask. Safe to log, audit and show."""

    STARTED = "scout.started"
    NOT_ALLOWED = "scout.not_allowed"
    BUSY = "scout.busy"  # a run is still going in this workspace


class ScoutControl:
    """Starts, moves and closes the scout's runs for one tenant; each through its workspace's handle."""

    def __init__(self, db: Database, tenant: UUID, clock: Callable[[], datetime]) -> None:
        self._db = db
        self._tenant = tenant
        self._clock = clock
        self._ledger = Ledger(db, tenant)

    def _runs(self, workspace: WorkspaceRow) -> ScoutRuns:
        # The workspace comes from the API door (the address the Chairman opened): this is its door.
        return ScoutRuns(self._db.scoped(self._tenant, workspace.workspace_id))

    async def start(  # the workspace, what is read, with what, by whom
        self, workspace: WorkspaceRow, *, repo: str, model: str, role: Role, channel: Channel
    ) -> tuple[ScoutVerdict, UUID | None]:
        """Start a run (audited first-hand). The run's id, or ``None`` with why not."""
        now = self._clock()
        run_id = uuid4()
        if not authorize(role, Operation.ASK_SCOUT, channel, step_up_at=None, now=now).allowed:
            verdict = ScoutVerdict.NOT_ALLOWED
        elif await self._runs(workspace).start(
            run_id, repo=repo, model=model, by=_ACTOR[role], at=now, stale_before=now - STALE_AFTER
        ):
            verdict = ScoutVerdict.STARTED
        else:
            verdict = ScoutVerdict.BUSY
        await self._audit(
            workspace, verdict, role, now, {"repo": repo, "model": model, "run_id": str(run_id)}
        )
        return verdict, run_id if verdict is ScoutVerdict.STARTED else None

    async def stage(
        self, workspace: WorkspaceRow, run_id: UUID, stage: Stage, *, branch: str | None = None
    ) -> None:
        """Record where a run is (shown as its progress note)."""
        await self._runs(workspace).stage(run_id, stage, branch=branch)

    async def finish(self, workspace: WorkspaceRow, run_id: UUID, report: dict[str, Any]) -> bool:
        """Close a run with its checked advice."""
        return await self._runs(workspace).finish(run_id, report, at=self._clock())

    async def fail(self, workspace: WorkspaceRow, run_id: UUID, reason: str) -> bool:
        """Close a run as failed, with a reason code."""
        return await self._runs(workspace).fail(run_id, reason, at=self._clock())

    async def latest(self, workspace: WorkspaceRow) -> ScoutRun | None:
        """The workspace's newest run."""
        return await self._runs(workspace).latest()

    async def _audit(
        self,
        workspace: WorkspaceRow,
        verdict: ScoutVerdict,
        role: Role,
        now: datetime,
        details: dict[str, Any],
    ) -> None:
        say(get_logger("agentzaaheen.scout"), "scout_asked", workspace=workspace.slug, verdict=verdict.value)
        await self._ledger.record(
            AuditDraft(
                at=now,
                actor=_ACTOR[role],
                action="workspaces.scout",
                outcome=AuditOutcome.ALLOW if verdict is ScoutVerdict.STARTED else AuditOutcome.DENY,
                reason_code=verdict.value,
                correlation_id=uuid4(),
                project_id=workspace.workspace_id,
                target_ref=f"workspace:{workspace.workspace_id}",
                details={"verdict": verdict.value, **details},
            )
        )

"""The scout's runs, per workspace (migration 0010, F17 slice 5b-2, ADR-039).

A run is started, moves through its stages (reading, thinking, checking) and
ends ``done`` with the checked advice or ``failed`` with a reason code. The
database allows one running run per workspace. A run left ``running`` by a
dashboard that stopped half way is closed as ``scout.interrupted`` when the
next one starts, if it is older than the caller's limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from yaqzan.core.store.db import Scoped, changed, require_aware
from yaqzan.core.store.errors import StoreError, StoreErrorCode

RunState = Literal["running", "done", "failed"]
Stage = Literal["reading", "thinking", "checking", "finished"]
INTERRUPTED = "scout.interrupted"

_CLOSE_STALE = """
UPDATE yaqzan.scout_runs SET state = 'failed', stage = 'finished', reason = $4, finished_at = $3
WHERE tenant_id = $1 AND project_id = $2 AND state = 'running' AND started_at < $5
"""
_START = """
INSERT INTO yaqzan.scout_runs
    (tenant_id, project_id, run_id, repo, model, state, stage, started_by, started_at)
VALUES ($1, $2, $3, $4, $5, 'running', 'reading', $6, $7)
"""
_STAGE = """
UPDATE yaqzan.scout_runs SET stage = $4, branch = coalesce($5, branch)
WHERE tenant_id = $1 AND project_id = $2 AND run_id = $3 AND state = 'running'
"""
_FINISH = """
UPDATE yaqzan.scout_runs SET state = $4, stage = 'finished', finished_at = $5, result = $6, reason = $7
WHERE tenant_id = $1 AND project_id = $2 AND run_id = $3 AND state = 'running'
"""
_LATEST = """
SELECT run_id, repo, branch, model, state, stage, started_by, started_at, finished_at, reason, result
FROM yaqzan.scout_runs WHERE tenant_id = $1 AND project_id = $2
ORDER BY started_at DESC, run_id LIMIT 1
"""


@dataclass(frozen=True)
class ScoutRun:
    """One run as saved: ``result`` is the checked advice (done), ``reason`` a code (failed)."""

    run_id: UUID
    repo: str
    branch: str | None
    model: str
    state: RunState
    stage: Stage
    started_by: str
    started_at: datetime
    finished_at: datetime | None
    reason: str | None
    result: dict[str, Any] | None


class ScoutRuns:
    """The scout's runs in the one workspace of its handle."""

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope
        self._key = (scope.tenant_id, scope.require_workspace())

    async def start(  # noqa: PLR0913  # the run, what it reads, who, when, and how old a run may be
        self, run_id: UUID, *, repo: str, model: str, by: str, at: datetime, stale_before: datetime
    ) -> bool:
        """Start a run; ``False`` if another one is still running in this workspace.

        A run still ``running`` but started before ``stale_before`` was left by a
        dashboard that stopped: it is closed as interrupted first.
        """
        require_aware(at, stale_before)
        try:
            async with self._scope.write("a scout run started") as conn:
                await conn.execute(_CLOSE_STALE, *self._key, at, INTERRUPTED, stale_before)
                await conn.execute(_START, *self._key, run_id, repo, model, by, at)
        except StoreError as exc:
            if exc.code is StoreErrorCode.CONFLICT:
                return False
            raise
        return True

    async def stage(self, run_id: UUID, stage: Stage, *, branch: str | None = None) -> None:
        """Move a running run to its next stage (and record the branch once it is known)."""
        async with self._scope.write("a scout run's stage") as conn:
            changed(await conn.execute(_STAGE, *self._key, run_id, stage, branch))

    async def finish(self, run_id: UUID, result: dict[str, Any], *, at: datetime) -> bool:
        """Close a run as done with its checked advice. ``False`` if it was not running any more."""
        require_aware(at)
        async with self._scope.write("a scout run's advice") as conn:
            return changed(await conn.execute(_FINISH, *self._key, run_id, "done", at, result, None))

    async def fail(self, run_id: UUID, reason: str, *, at: datetime) -> bool:
        """Close a run as failed with a reason code. ``False`` if it was not running any more."""
        require_aware(at)
        async with self._scope.write("a failed scout run") as conn:
            return changed(await conn.execute(_FINISH, *self._key, run_id, "failed", at, None, reason))

    async def latest(self) -> ScoutRun | None:
        """The newest run in this workspace, or ``None``."""
        async with self._scope.connection() as conn:
            row = await conn.fetchrow(_LATEST, *self._key)
        return None if row is None else ScoutRun(**dict(row))

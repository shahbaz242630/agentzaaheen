"""Removing a project against a real Postgres: archive, bring back, delete for good (F17 slice 4).

Archiving switches everything off, deletes the saved keys and keeps the
history; nothing new lands in an archived workspace. Bringing back leaves it
switched off. Deleting for good is only possible once archived, removes every
project-level row and leaves only a marker; the audit log keeps the record.
Every move needs a fresh passkey step-up, and the database keeps the same
rules underneath the code.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest

from tests.integration.conftest import ScratchDb, add_workspaces, connect_as, open_db, run
from tests.integration.test_policy_agents import ME, TENANT
from tests.integration.test_store_records import run_record
from tests.unit.test_registry_roles import pack
from yaqzan.core.policy import (
    AgentControl,
    AgentVerdict,
    Channel,
    LifecycleVerdict,
    Role,
    WorkspaceControl,
)
from yaqzan.core.registry.roles import RoleLibrary, load_roles
from yaqzan.core.store import (
    AgentRows,
    AuditRows,
    Database,
    Records,
    SealedRow,
    SealedSecrets,
    SlotKey,
    StoreError,
    WorkspaceRow,
    WorkspaceRows,
    WorkspaceSources,
)

EVA = UUID("00000000-0000-4000-8000-0000000000f2")
ZAAHEEN = UUID("00000000-0000-4000-8000-0000000000f3")
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
PROJECT_TABLES = (
    "events", "runs", "findings", "actions", "sealed_secrets", "revenue_subscriptions",
    "revenue_payments", "revenue_refunds", "source_syncs", "workspace_settings", "agents",
    "workspace_sources",
)  # fmt: skip


def _hello(n: int) -> datetime:
    """The time of the n-th Windows Hello approval: each move needs its own."""
    return NOW - timedelta(seconds=60 - n)


@pytest.fixture(autouse=True)
def _workspaces(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT, EVA, ZAAHEEN))


@pytest.fixture
def library(tmp_path: Path) -> RoleLibrary:
    pack(tmp_path, "monitoring")
    return load_roles(tmp_path)


def removal(db: Database) -> WorkspaceControl:
    return WorkspaceControl(db, TENANT, lambda: NOW)


def agents(db: Database, library: RoleLibrary) -> AgentControl:
    return AgentControl(db, TENANT, library, lambda: NOW)


def _key(workspace: UUID) -> SealedRow:
    return SealedRow(
        slot=SlotKey(TENANT, workspace, "paddle", "api_key", "staging"),
        format_version=1,
        master_key_id="k1",
        wrapped_dek=bytes(60),  # sizes the vault produces; the bytes themselves are never opened here
        ciphertext=bytes(29),
    )


async def _busy(db: Database, library: RoleLibrary, workspace: UUID) -> UUID:
    """A workspace in use: an agent switched on with a cap, the workspace on, a saved key and a run."""
    rules = agents(db, library)
    assert await rules.give(workspace, "monitoring", **ME) is AgentVerdict.DONE
    (view,) = (await rules.view(workspace)).agents
    agent = view.agent.agent_id
    await rules.set_workspace(workspace, enabled=True, cap_minor=1000, **ME, step_up_at=_hello(1))
    await rules.set_agent(workspace, agent, enabled=True, cap_minor=500, **ME, step_up_at=_hello(2))
    await SealedSecrets(db.scoped(TENANT, workspace)).put(_key(workspace))
    await WorkspaceSources(db.scoped(TENANT, workspace)).connect_github(
        f"owner/busy-{workspace.hex[-4:]}", by="user:chairman", at=NOW
    )
    await Records(db.scoped(TENANT, workspace)).add_run(
        run_record(
            project_id=workspace, tenant_id=TENANT, agent=str(agent), cost_minor_units=10, started_at=NOW
        )
    )
    return agent


async def _counts(admin_url: str, workspace: UUID) -> dict[str, int]:
    """Rows per project-level table, seen past the walls (the test's admin), never by the app."""
    conn = await asyncpg.connect(admin_url)
    try:
        return {
            # ``t`` comes from the fixed list above, never from outside.
            t: await conn.fetchval(f"SELECT count(*) FROM yaqzan.{t} WHERE project_id = $1", workspace)  # noqa: S608
            for t in PROJECT_TABLES
        }
    finally:
        await conn.close()


def _ids(rows: Sequence[WorkspaceRow]) -> set[UUID]:
    return {r.workspace_id for r in rows}


def test_archiving_switches_everything_off_deletes_the_keys_and_keeps_the_history(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            agent = await _busy(db, library, EVA)
            await _busy(db, library, ZAAHEEN)
            done = await removal(db).archive(EVA, **ME, step_up_at=_hello(3))
            assert done.verdict is LifecycleVerdict.DONE
            assert done.archived is not None
            assert (done.archived.agents_stopped, done.archived.keys_deleted) == (1, 1)

            rows = AgentRows(db.scoped(TENANT, EVA))
            (stopped,) = await rows.agents()
            assert (stopped.agent_id, stopped.enabled, stopped.cap_minor) == (agent, False, None)
            settings = await rows.settings()
            assert settings is not None
            assert (settings.enabled, settings.cap_minor) == (False, None)
            assert _ids(await removal(db).active()) == {ZAAHEEN}
            assert _ids(await removal(db).archived()) == {EVA}
            # Another workspace is untouched.
            assert (await agents(db, library).may_run(ZAAHEEN, (await AgentRows(
                db.scoped(TENANT, ZAAHEEN)).agents())[0].agent_id)).allowed  # fmt: skip

        counts = await _counts(test_db.admin_url, EVA)
        assert counts["sealed_secrets"] == 0
        assert counts["runs"] == 1  # the history is kept
        assert counts["workspace_sources"] == 1  # still its repository: bringing it back needs no new link
        assert (await _counts(test_db.admin_url, ZAAHEEN))["sealed_secrets"] == 1

    run(body())


def test_nothing_new_lands_in_an_archived_workspace(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            agent = await _busy(db, library, EVA)
            await removal(db).archive(EVA, **ME, step_up_at=_hello(3))
            with pytest.raises(StoreError):
                await Records(db.scoped(TENANT, EVA)).add_run(
                    run_record(project_id=EVA, tenant_id=TENANT, agent="x", started_at=NOW)
                )
            with pytest.raises(StoreError):
                await SealedSecrets(db.scoped(TENANT, EVA)).put(_key(EVA))
            with pytest.raises(StoreError):
                await agents(db, library).set_agent(
                    EVA, agent, enabled=True, cap_minor=5, **ME, step_up_at=_hello(4)
                )
            assert not (await agents(db, library).may_run(EVA, agent)).allowed

    run(body())


def test_bringing_back_leaves_it_switched_off_with_no_keys(test_db: ScratchDb, library: RoleLibrary) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            agent = await _busy(db, library, EVA)
            await removal(db).archive(EVA, **ME, step_up_at=_hello(3))
            assert await removal(db).restore(EVA, **ME, step_up_at=_hello(4)) is LifecycleVerdict.DONE
            assert EVA in _ids(await removal(db).active())
            assert not (await agents(db, library).may_run(EVA, agent)).allowed
            assert await SealedSecrets(db.scoped(TENANT, EVA)).get(_key(EVA).slot) is None
            # Once back, it can be used again (with a fresh passkey, as always).
            await agents(db, library).set_workspace(
                EVA, enabled=True, cap_minor=100, **ME, step_up_at=_hello(5)
            )
            assert await removal(db).restore(EVA, **ME, step_up_at=_hello(6)) is LifecycleVerdict.NOT_ARCHIVED

    run(body())


def test_deleting_for_good_needs_it_archived_first_and_keeps_the_audit_log(
    test_db: ScratchDb, library: RoleLibrary
) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await _busy(db, library, EVA)
            await _busy(db, library, ZAAHEEN)  # must keep everything
            assert await removal(db).erase(EVA, **ME, step_up_at=_hello(3)) is LifecycleVerdict.NOT_ARCHIVED
            await removal(db).archive(EVA, **ME, step_up_at=_hello(4))
            assert await removal(db).erase(EVA, **ME, step_up_at=_hello(5)) is LifecycleVerdict.DONE
            assert EVA not in _ids(await removal(db).active()) | _ids(await removal(db).archived())
            # Setting up the laptop again does not bring it back.
            assert not await WorkspaceRows(db.scoped(TENANT)).add(WorkspaceRow(EVA, f"w-{EVA.hex}", "Eva"))
            assert EVA not in _ids(await removal(db).active())
            assert await removal(db).restore(EVA, **ME, step_up_at=_hello(6)) is LifecycleVerdict.NOT_ARCHIVED
            audit = await AuditRows(db.scoped(TENANT)).all_rows()

        assert set((await _counts(test_db.admin_url, EVA)).values()) == {0}
        assert sum((await _counts(test_db.admin_url, ZAAHEEN)).values()) > 0
        moves = [
            (e.action, e.reason_code)
            for e in audit
            if e.project_id == EVA and e.action.startswith("workspaces.")
        ]
        assert moves == [
            ("workspaces.erase", "workspaces.not_archived"),
            ("workspaces.archive", "workspaces.done"),
            ("workspaces.erase", "workspaces.done"),
            ("workspaces.restore", "workspaces.not_archived"),
        ]
        assert any(e.project_id == EVA and e.action == "agents.give" for e in audit)  # older history stays

    run(body())


def test_every_move_needs_the_chairman_with_a_fresh_passkey(test_db: ScratchDb) -> None:
    async def body() -> list[LifecycleVerdict]:
        async with open_db(test_db.app_url) as db:
            rules = removal(db)
            return [
                (await rules.archive(EVA, **ME, step_up_at=None)).verdict,
                (await rules.archive(EVA, **ME, step_up_at=NOW - timedelta(hours=1))).verdict,
                (
                    await rules.archive(EVA, role=Role.VIEWER, channel=Channel.DASHBOARD, step_up_at=NOW)
                ).verdict,
                (
                    await rules.archive(EVA, role=Role.CHAIRMAN, channel=Channel.TELEGRAM, step_up_at=NOW)
                ).verdict,
                await rules.erase(EVA, **ME, step_up_at=None),
                await rules.restore(EVA, **ME, step_up_at=None),
                await rules.erase(EVA, **ME, step_up_at=NOW - timedelta(hours=1)),
                await rules.restore(EVA, **ME, step_up_at=NOW - timedelta(hours=1)),
            ]

    assert run(body()) == [
        LifecycleVerdict.NEEDS_PASSKEY,
        LifecycleVerdict.NOT_ALLOWED,
        LifecycleVerdict.NOT_ALLOWED,
        LifecycleVerdict.NOT_ALLOWED,
        LifecycleVerdict.NEEDS_PASSKEY,
        LifecycleVerdict.NEEDS_PASSKEY,
        LifecycleVerdict.NOT_ALLOWED,
        LifecycleVerdict.NOT_ALLOWED,
    ]


@pytest.mark.parametrize(
    "statements",
    [
        # Archived without a step-up, or by the system.
        [
            (
                "UPDATE yaqzan.workspaces SET state = 'archived',"
                " state_changed_by = 'user:chairman'"
                " WHERE workspace_id = $1",
                EVA,
            ),
        ],
        [
            (
                "UPDATE yaqzan.workspaces SET state = 'archived',"
                " state_changed_by = 'system:yaqzan',"
                " step_up_at = now()"
                " WHERE workspace_id = $1",
                EVA,
            ),
        ],
        # Straight from active to deleted: it must be archived first.
        [
            (
                "UPDATE yaqzan.workspaces SET state = 'deleted',"
                " state_changed_by = 'user:chairman',"
                " step_up_at = now()"
                " WHERE workspace_id = $1",
                EVA,
            ),
        ],
        # The same step-up used for two moves.
        [
            (
                "UPDATE yaqzan.workspaces SET state = 'archived',"
                " state_changed_by = 'user:chairman',"
                " step_up_at = '2026-10-24T09:00:00Z'"
                " WHERE workspace_id = $1",
                EVA,
            ),
            (
                "UPDATE yaqzan.workspaces SET state = 'active',"
                " state_changed_by = 'user:chairman',"
                " step_up_at = '2026-10-24T09:00:00Z'"
                " WHERE workspace_id = $1",
                EVA,
            ),
        ],
        # Deleting an active workspace through the function.
        [
            (
                "SELECT yaqzan.erase_workspace($1, 'user:chairman', now(), now())",
                EVA,
            ),
        ],
        # Deleting another workspace than the one this connection is for.
        [
            (
                "SELECT yaqzan.erase_workspace($1, 'user:chairman', now(), now())",
                ZAAHEEN,
            ),
        ],
        # A deleted workspace never comes back.
        [
            (
                "UPDATE yaqzan.workspaces SET state = 'archived',"
                " state_changed_by = 'user:chairman',"
                " step_up_at = now() - interval '1 minute'"
                " WHERE workspace_id = $1",
                EVA,
            ),
            (
                "SELECT yaqzan.erase_workspace($1, 'user:chairman', now(), now())",
                EVA,
            ),
            (
                "UPDATE yaqzan.workspaces SET state = 'active',"
                " state_changed_by = 'user:chairman',"
                " step_up_at = now() + interval '1 minute'"
                " WHERE workspace_id = $1",
                EVA,
            ),
        ],
    ],
)
def test_the_database_refuses_moves_written_around_the_code(
    test_db: ScratchDb, statements: list[tuple[str, UUID]]
) -> None:
    async def body() -> None:
        conn = await connect_as(test_db.app_url, TENANT, EVA)
        try:
            async with conn.transaction():
                for sql, target in statements:
                    await conn.execute(sql, target)
        finally:
            await conn.close()

    with pytest.raises((asyncpg.CheckViolationError, asyncpg.InsufficientPrivilegeError)):
        run(body())


def test_the_app_cannot_delete_history_except_through_the_function(test_db: ScratchDb) -> None:
    async def body() -> None:
        conn = await connect_as(test_db.app_url, TENANT, EVA)
        try:
            await conn.execute("DELETE FROM yaqzan.runs WHERE project_id = $1", EVA)
        finally:
            await conn.close()

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        run(body())


def test_a_tenant_cannot_archive_another_tenants_workspace(test_db: ScratchDb) -> None:
    other = UUID("00000000-0000-4000-8000-0000000000f9")

    async def body() -> LifecycleVerdict:
        async with open_db(test_db.app_url) as db:
            await WorkspaceRows(db.scoped(other)).add_tenant("Other")
            return (await WorkspaceControl(db, other, lambda: NOW).archive(EVA, **ME, step_up_at=NOW)).verdict

    assert run(body()) is LifecycleVerdict.NOT_ACTIVE  # behind the wall, there is no such workspace

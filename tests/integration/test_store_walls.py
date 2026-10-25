"""The database walls between tenants and workspaces (ADR-039, F17 slice 1).

Research 10a: four open-source AI apps leaked between users or tenants, almost
always through one query that forgot its filter. These tests hold Postgres
itself to the rule, whatever the query says:

- every table (but the migrations list) has a wall, enabled and forced;
- a query that names no tenant, or no workspace for a project-level table, is refused;
- a row written in workspace A is invisible from workspace B and from another
  tenant, in **every** table, even to a query with no filter at all;
- a row cannot be written into a workspace other than the one named.

A new table without a wall, or one this file forgot to fill, fails here.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import asyncpg
import pytest

from agentzaaheen.core.policy import Ledger
from agentzaaheen.core.store import (
    AgentRows,
    AllSealedSecrets,
    Briefings,
    DailySummaries,
    KillSwitchRows,
    MissRow,
    ModelCallMisses,
    ModelCalls,
    NamePrints,
    NewAgent,
    PasskeyRow,
    PasskeyRows,
    Records,
    RegistrySwitchRows,
    RevenueRows,
    RoleVersion,
    RoleVersionRows,
    ScoutRuns,
    SealedRow,
    SealedSecrets,
    SlotKey,
    StoreError,
    StoreErrorCode,
    SwitchScope,
    WorkspaceRows,
    WorkspaceSettings,
    WorkspaceSources,
)
from agentzaaheen.core.store.db import _SET_SCOPE as SET_SCOPE  # the exact statement the store runs
from tests.integration.conftest import ScratchDb, add_workspaces, connect_as, open_db, run
from tests.integration.test_policy_ledger import draft
from tests.integration.test_store_records import NOW, PROJECT, TENANT, action, event, finding, run_record
from tests.integration.test_store_registry_switches import CHAIRMAN, request
from tests.integration.test_store_revenue import AT, full_batch

OTHER_WORKSPACE = UUID("00000000-0000-4000-8000-0000000000bb")
OTHER_TENANT = UUID("00000000-0000-4000-8000-000000000002")

_TABLES = """
SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
    (SELECT count(*) FROM pg_policies p
        WHERE p.schemaname = 'agentzaaheen' AND p.tablename = c.relname) AS policies,
    EXISTS (SELECT 1 FROM information_schema.columns k WHERE k.table_schema = 'agentzaaheen'
        AND k.table_name = c.relname AND k.column_name = 'project_id'
        AND k.is_nullable = 'NO') AS per_workspace
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'agentzaaheen' AND c.relkind = 'r' AND c.relname <> 'schema_migrations'
ORDER BY c.relname
"""


def _tables(test_db: ScratchDb) -> list[asyncpg.Record]:
    async def body() -> list[asyncpg.Record]:
        conn = await asyncpg.connect(test_db.admin_url)
        try:
            return await conn.fetch(_TABLES)
        finally:
            await conn.close()

    return run(body())


async def _fill_every_table(url: str) -> None:
    """At least one row in every table, all in tenant ``TENANT``, workspace ``PROJECT``."""
    await add_workspaces(url, TENANT, PROJECT, OTHER_WORKSPACE)
    async with open_db(url) as db:
        here = db.scoped(TENANT, PROJECT)
        records = Records(here)
        await records.add_event(event())
        parent = run_record()
        await records.add_run(parent)
        await records.add_finding(finding(parent.id))
        await records.add_action(action(parent.id))
        await SealedSecrets(here).put(
            SealedRow(
                slot=SlotKey(TENANT, PROJECT, "paddle", "api_key", "production"),
                format_version=1,
                master_key_id="k1",
                wrapped_dek=b"d" * 60,
                ciphertext=b"c" * 29,
            )
        )
        await RevenueRows(here).save_batch(full_batch(), at=AT)
        await KillSwitchRows(db.scoped(TENANT)).set(
            SwitchScope.ALL, "", engaged=False, changed_by=CHAIRMAN, changed_at=NOW
        )
        await Ledger(db, TENANT).record(draft(0))
        registry = RegistrySwitchRows(db.scoped(TENANT))
        asked = request()
        await registry.add_request(asked)
        await registry.approve(asked.id, by=CHAIRMAN, at=NOW, step_up_at=NOW)
        await PasskeyRows(db.scoped(TENANT)).add_first(
            PasskeyRow(
                credential_id=b"i" * 16,
                public_key=b"k" * 64,
                sign_count=0,
                created_by=CHAIRMAN,
                created_at=NOW,
            ),
        )
        await RoleVersionRows(db.scoped(TENANT)).put(
            RoleVersion("monitoring", "f" * 64, {"ROLE.md": "# Monitoring"}, "read", NOW)
        )
        agents = AgentRows(here)
        await agents.add(NewAgent(uuid4(), "monitoring", "f" * 64, "local-gemma", by=CHAIRMAN, at=NOW))
        await agents.put_settings(WorkspaceSettings(False, None, CHAIRMAN, NOW, None))
        await WorkspaceSources(here).connect_github("owner/walled", by=CHAIRMAN, at=NOW)
        await ScoutRuns(here).start(
            uuid4(), repo="owner/walled", model="local-gemma", by=CHAIRMAN, at=NOW, stale_before=NOW
        )
        await DailySummaries(here).put(NOW.date(), {"walled": True}, made_at=NOW)
        await Briefings(db.scoped(TENANT)).open(NOW.date(), title="Briefing", message="Walled.", made_at=NOW)
        await ModelCalls(db.scoped(TENANT)).add("heads", "claude-opus-5-5", 1, at=NOW)
        await ModelCallMisses(db.scoped(TENANT)).add(
            MissRow(NOW, "heads", "heads", "claude-opus-5-5", "model.declined", None, "run-1")
        )
        await NamePrints(here).add({("single", bytes(32))}, key_id="names-1", at=NOW)


async def _count(url: str, table: str, tenant: UUID, workspace: UUID | None) -> int:
    conn = await connect_as(url, tenant, workspace)
    try:
        # No WHERE clause on purpose: the wall alone decides what is seen. Names come from the catalog.
        return int(await conn.fetchval(f"SELECT count(*) FROM agentzaaheen.{table}"))  # noqa: S608
    finally:
        await conn.close()


def test_every_table_has_a_wall_that_is_on_and_forced(test_db: ScratchDb) -> None:
    tables = _tables(test_db)
    assert len(tables) >= 16  # the check below would pass vacuously if this found nothing
    missing = [
        row["relname"]
        for row in tables
        if not (row["relrowsecurity"] and row["relforcerowsecurity"] and row["policies"] >= 1)
    ]
    assert missing == []


def test_a_row_in_one_workspace_is_invisible_everywhere_else(test_db: ScratchDb) -> None:
    run(_fill_every_table(test_db.app_url))
    seen: dict[str, tuple[int, int, int]] = {}
    for row in _tables(test_db):
        table = row["relname"]
        own = run(_count(test_db.app_url, table, TENANT, PROJECT if row["per_workspace"] else None))
        next_door = (
            run(_count(test_db.app_url, table, TENANT, OTHER_WORKSPACE)) if row["per_workspace"] else 0
        )
        stranger = run(_count(test_db.app_url, table, OTHER_TENANT, PROJECT))
        seen[table] = (own, next_door, stranger)
    empty = sorted(t for t, (own, _, _) in seen.items() if own == 0)
    assert empty == []  # every table was filled, so the zeros below mean something
    leaks = {t: counts for t, counts in seen.items() if counts[1] or counts[2]}
    assert leaks == {}


def test_a_query_that_names_no_tenant_is_refused(test_db: ScratchDb) -> None:
    run(_fill_every_table(test_db.app_url))

    async def count_without_a_tenant(table: str) -> None:
        conn = await asyncpg.connect(test_db.app_url)
        try:
            await conn.fetchval(f"SELECT count(*) FROM agentzaaheen.{table}")  # noqa: S608  # name from the catalog
        finally:
            await conn.close()

    allowed = []
    for row in _tables(test_db):
        try:
            run(count_without_a_tenant(row["relname"]))
            allowed.append(row["relname"])
        except asyncpg.InsufficientPrivilegeError:
            pass
    assert allowed == []


def test_a_project_table_needs_a_workspace_too(test_db: ScratchDb) -> None:
    run(_fill_every_table(test_db.app_url))
    per_workspace = [row["relname"] for row in _tables(test_db) if row["per_workspace"]]
    assert len(per_workspace) >= 9
    allowed = []
    for table in per_workspace:
        try:
            run(_count(test_db.app_url, table, TENANT, None))
            allowed.append(table)
        except asyncpg.InsufficientPrivilegeError:
            pass
    assert allowed == []


def test_a_row_cannot_be_written_into_another_workspace(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT, PROJECT, OTHER_WORKSPACE))

    async def body() -> None:
        async with open_db(test_db.app_url) as db, db.scoped(PROJECT).connection() as conn:
            await conn.execute(
                "INSERT INTO agentzaaheen.source_syncs (tenant_id, project_id, source, last_attempt_at)"
                " VALUES ($1, $2, 'paddle', now())",
                TENANT,
                OTHER_WORKSPACE,
            )

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.DENIED


def test_a_row_cannot_be_written_for_another_tenant(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db, db.scoped(TENANT).connection() as conn:
            await conn.execute(
                "INSERT INTO agentzaaheen.kill_switches (tenant_id, scope, ref, engaged, changed_by,"
                " changed_at)"
                " VALUES ($1, 'all', '', true, 'user:chairman', now())",
                OTHER_TENANT,
            )

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.DENIED


def test_a_row_needs_a_workspace_that_exists(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT))
    unknown = uuid4()

    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await RevenueRows(db.scoped(TENANT, unknown)).mark_attempt("paddle", at=NOW)

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.REJECTED


def test_the_scope_setting_itself_ends_with_its_transaction(test_db: ScratchDb) -> None:
    """The first of two layers: the setting is transaction-local (the pool's reset is the second)."""

    async def body() -> tuple[str | None, str | None]:
        conn = await asyncpg.connect(test_db.app_url)
        try:
            async with conn.transaction():
                await conn.execute(SET_SCOPE, str(TENANT), str(PROJECT))
            return (
                await conn.fetchval("SELECT current_setting('app.tenant_id', true)"),
                await conn.fetchval("SELECT current_setting('app.workspace_id', true)"),
            )
        finally:
            await conn.close()

    assert run(body()) == ("", "")


def test_the_scope_ends_with_its_transaction(test_db: ScratchDb) -> None:
    """A pooled connection handed back must not keep the last borrower's tenant."""
    run(_fill_every_table(test_db.app_url))

    async def body() -> int:
        async with open_db(test_db.app_url, max_size=1) as db:
            async with db.scoped(PROJECT).connection() as conn:
                await conn.fetchval("SELECT count(*) FROM agentzaaheen.events")
            async with db.connection() as conn:  # the very same connection, no scope named
                return int(await conn.fetchval("SELECT count(*) FROM agentzaaheen.events"))

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.DENIED


def test_tenant_wide_key_reads_cover_every_workspace_and_only_this_tenant(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT, PROJECT, OTHER_WORKSPACE))
    run(add_workspaces(test_db.app_url, OTHER_TENANT, PROJECT))

    def sealed(tenant: UUID, workspace: UUID) -> SealedRow:
        return SealedRow(
            slot=SlotKey(tenant, workspace, "paddle", "api_key", "production"),
            format_version=1,
            master_key_id="old",
            wrapped_dek=b"d" * 60,
            ciphertext=b"c" * 29,
            expires_at=NOW + timedelta(days=30),
        )

    async def body() -> tuple[set[tuple[UUID, UUID]], set[tuple[UUID, UUID]]]:
        async with open_db(test_db.app_url) as db:
            for tenant, workspace in ((TENANT, PROJECT), (TENANT, OTHER_WORKSPACE), (OTHER_TENANT, PROJECT)):
                await SealedSecrets(db.scoped(tenant, workspace)).put(sealed(tenant, workspace))
            keys = AllSealedSecrets(db.scoped(TENANT))
            dated = {(d.slot.tenant_id, d.slot.project_id) for d in await keys.dates()}
            stale = {(r.slot.tenant_id, r.slot.project_id) for r in await keys.not_under_key("new")}
            return dated, stale

    dated, stale = run(body())
    assert dated == stale == {(TENANT, PROJECT), (TENANT, OTHER_WORKSPACE)}


def test_workspaces_list_only_their_own_tenant(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT, PROJECT, OTHER_WORKSPACE))
    run(add_workspaces(test_db.app_url, OTHER_TENANT, uuid4()))

    async def body() -> set[UUID]:
        async with open_db(test_db.app_url) as db:
            return {w.workspace_id for w in await WorkspaceRows(db.scoped(TENANT)).workspaces()}

    assert run(body()) == {PROJECT, OTHER_WORKSPACE}


def test_workspaces_are_listed_in_the_order_they_were_added(test_db: ScratchDb) -> None:
    first, second, third = (UUID(f"00000000-0000-4000-8000-0000000000{n}") for n in ("c3", "a1", "b2"))
    run(add_workspaces(test_db.app_url, TENANT, first, second, third))

    async def body() -> list[UUID]:
        async with open_db(test_db.app_url) as db:
            return [w.workspace_id for w in await WorkspaceRows(db.scoped(TENANT)).workspaces()]

    assert run(body()) == [first, second, third]

"""Migrations: applied once, all-or-nothing, and a changed past migration is refused."""

from __future__ import annotations

import asyncio
from uuid import UUID

import asyncpg
import pytest

from agentzaaheen.core.store import (
    LATEST_VERSION,
    MIGRATIONS,
    Migration,
    StoreError,
    StoreErrorCode,
    check_schema,
    migrate,
)
from agentzaaheen.core.store.migrations import _APPLIED, _BOOTSTRAP, legacy_checksum
from tests.integration.conftest import OWNER, ScratchDb, drop_database, empty_database, open_db, run

TENANT = UUID("00000000-0000-4000-8000-0000000000a9")


def test_template_is_at_the_latest_version_and_migrating_again_does_nothing(test_db: ScratchDb) -> None:
    async def body() -> list[int]:
        async with open_db(test_db.owner_url) as db:
            await check_schema(db)
            return await migrate(db)

    assert run(body()) == []
    assert MIGRATIONS[-1].version == LATEST_VERSION


def test_app_can_check_the_schema_at_start_up(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await check_schema(db)

    run(body())


def test_changed_past_migration_is_refused(test_db: ScratchDb) -> None:
    edited = (Migration(1, MIGRATIONS[0].name, MIGRATIONS[0].sql + "\n-- quietly edited"), *MIGRATIONS[1:])

    async def body() -> None:
        async with open_db(test_db.owner_url) as db:
            await migrate(db, edited)

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.MIGRATION_CHANGED


def test_database_ahead_of_the_code_is_refused(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.owner_url) as db:
            await migrate(db, (*MIGRATIONS, Migration(LATEST_VERSION + 1, "future", "SELECT 1")))
            await check_schema(db)  # this code only knows MIGRATIONS

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.SCHEMA_MISMATCH


def test_code_ahead_of_the_database_is_refused(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.owner_url) as db:
            await check_schema(db, (*MIGRATIONS, Migration(LATEST_VERSION + 1, "future", "SELECT 1")))

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.SCHEMA_MISMATCH


def test_empty_database_is_refused_at_start_up(test_db: ScratchDb) -> None:
    name = empty_database(test_db.server)
    try:

        async def body() -> None:
            async with open_db(test_db.server.url(OWNER, test_db.server.owner_password, name)) as db:
                await check_schema(db)

        with pytest.raises(StoreError) as caught:
            run(body())
        assert caught.value.code is StoreErrorCode.SCHEMA_MISMATCH
    finally:
        drop_database(test_db.server, name)


def test_two_migrators_at_once_apply_each_migration_once(test_db: ScratchDb) -> None:
    name = empty_database(test_db.server)
    try:

        async def body() -> list[list[int]]:
            async with open_db(test_db.server.url(OWNER, test_db.server.owner_password, name)) as db:
                return list(await asyncio.gather(migrate(db), migrate(db)))

        results = run(body())
        assert sorted(results) == [[], [m.version for m in MIGRATIONS]]
    finally:
        drop_database(test_db.server, name)


def test_a_failing_migration_leaves_nothing_behind(test_db: ScratchDb) -> None:
    broken = (
        *MIGRATIONS,
        Migration(LATEST_VERSION + 1, "broken", "CREATE TABLE agentzaaheen.half (id int); SELECT 1/0;"),
    )

    async def body() -> bool:
        async with open_db(test_db.owner_url) as db:
            with pytest.raises(StoreError):
                await migrate(db, broken)
            async with db.connection() as conn:
                return bool(await conn.fetchval("SELECT to_regclass('agentzaaheen.half') IS NULL"))

    assert run(body()) is True


def _legacy(sql: str) -> str:
    return sql.replace("agentzaaheen", "yaqzan")  # the text as it was before the rename (25 Oct 2026)


async def _build_legacy(url: str, upto: int) -> None:
    """A database as the old code made it: schema, role and history all under the old name."""
    conn = await asyncpg.connect(url)
    try:
        async with conn.transaction():
            await conn.execute(_legacy(_BOOTSTRAP))
            for m in MIGRATIONS[:upto]:
                await conn.execute(_legacy(m.sql))
                await conn.execute(
                    "INSERT INTO yaqzan.schema_migrations (version, name, checksum) VALUES ($1, $2, $3)",
                    m.version, m.name, legacy_checksum(m),
                )  # fmt: skip
    finally:
        await conn.close()


def test_a_database_made_under_the_old_name_is_upgraded_once_and_keeps_its_data(test_db: ScratchDb) -> None:
    name = empty_database(test_db.server)
    url = test_db.server.url(OWNER, test_db.server.owner_password, name)

    async def body() -> None:
        await _build_legacy(url, upto=11)
        conn = await asyncpg.connect(url)
        try:
            async with conn.transaction():  # the tenant wall holds for the owner too
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(TENANT))
                await conn.execute("INSERT INTO yaqzan.tenants (tenant_id, name) VALUES ($1, 'Kept')", TENANT)
        finally:
            await conn.close()
        async with open_db(url) as db:
            assert await migrate(db) == [12, 13]  # the old history is accepted, only the new ones run
            assert await migrate(db) == []
            await check_schema(db)
        conn = await asyncpg.connect(url)
        try:
            schemas = await conn.fetch(
                "SELECT nspname FROM pg_namespace WHERE nspname IN ('yaqzan', 'agentzaaheen')"
            )
            assert [r["nspname"] for r in schemas] == ["agentzaaheen"]
            async with conn.transaction():  # read back through the same wall, under the new name
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(TENANT))
                kept = await conn.fetchval(
                    "SELECT name FROM agentzaaheen.tenants WHERE tenant_id = $1", TENANT
                )
            assert kept == "Kept"
            stale = await conn.fetchval(
                "SELECT count(*) FROM (SELECT p.oid FROM pg_proc p"
                " JOIN pg_namespace n ON n.oid = p.pronamespace"
                " WHERE n.nspname = 'agentzaaheen' AND p.prokind IN ('f', 'p') OFFSET 0) ours"
                " WHERE pg_get_functiondef(ours.oid) LIKE '%yaqzan%'"
            )
            assert stale == 0  # every function body now names the new schema
            recorded = {r["version"]: r["checksum"] for r in await conn.fetch(_APPLIED)}
            assert recorded == {m.version: m.checksum for m in MIGRATIONS}
        finally:
            await conn.close()

    try:
        run(body())
    finally:
        drop_database(test_db.server, name)


def test_an_old_history_that_differs_in_more_than_the_name_is_still_refused(test_db: ScratchDb) -> None:
    name = empty_database(test_db.server)
    url = test_db.server.url(OWNER, test_db.server.owner_password, name)

    async def body() -> None:
        await _build_legacy(url, upto=2)
        conn = await asyncpg.connect(url)
        try:
            await conn.execute("UPDATE yaqzan.schema_migrations SET checksum = 'edited' WHERE version = 2")
        finally:
            await conn.close()
        async with open_db(url) as db:
            await migrate(db)

    try:
        with pytest.raises(StoreError) as caught:
            run(body())
        assert caught.value.code is StoreErrorCode.MIGRATION_CHANGED
    finally:
        drop_database(test_db.server, name)

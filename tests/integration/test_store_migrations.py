"""Migrations: applied once, all-or-nothing, and a changed past migration is refused."""

from __future__ import annotations

import asyncio

import pytest

from tests.integration.conftest import OWNER, ScratchDb, drop_database, empty_database, open_db, run
from yaqzan.core.store import (
    LATEST_VERSION,
    MIGRATIONS,
    Migration,
    StoreError,
    StoreErrorCode,
    check_schema,
    migrate,
)


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
        Migration(LATEST_VERSION + 1, "broken", "CREATE TABLE yaqzan.half (id int); SELECT 1/0;"),
    )

    async def body() -> bool:
        async with open_db(test_db.owner_url) as db:
            with pytest.raises(StoreError):
                await migrate(db, broken)
            async with db.connection() as conn:
                return bool(await conn.fetchval("SELECT to_regclass('yaqzan.half') IS NULL"))

    assert run(body()) is True

"""Least privilege: what the app role and even the owner can NOT do (SECURITY SP-9, CR-1)."""

from __future__ import annotations

import secrets
from uuid import uuid4

import asyncpg
import pytest
from pydantic import SecretStr

from tests.integration.conftest import OWNER, ScratchDb, drop_database, empty_database, open_db, run
from tests.integration.test_policy_ledger import TENANT, draft
from yaqzan.core.policy import Ledger
from yaqzan.core.store import Database, DatabaseSettings, StoreError, StoreErrorCode, migrate


async def _app_executes(url: str, statement: str) -> None:
    async with open_db(url) as db:
        await Ledger(db, TENANT).record(draft(0))
        async with db.connection() as conn:
            await conn.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE yaqzan.audit_log SET reason_code = 'x.y'",
        "DELETE FROM yaqzan.audit_log",
        "TRUNCATE yaqzan.audit_log",
        "UPDATE yaqzan.events SET trust = 'trusted'",
        "DELETE FROM yaqzan.runs",
        "UPDATE yaqzan.actions SET status = 'approved'",
        "DELETE FROM yaqzan.kill_switches",
        "DELETE FROM yaqzan.revenue_subscriptions",
        "DELETE FROM yaqzan.revenue_payments",
        "DELETE FROM yaqzan.revenue_refunds",
        "DELETE FROM yaqzan.source_syncs",
        "TRUNCATE yaqzan.revenue_payments",
        "INSERT INTO yaqzan.schema_migrations (version, name, checksum) VALUES (99, 'x', 'y')",
        "CREATE TABLE yaqzan.sneaky (id int)",
        "ALTER TABLE yaqzan.audit_log DISABLE TRIGGER ALL",
        "SET session_replication_role = replica",
    ],
)
def test_app_role_is_refused(test_db: ScratchDb, statement: str) -> None:
    with pytest.raises(StoreError) as caught:
        run(_app_executes(test_db.app_url, statement))
    assert caught.value.code is StoreErrorCode.DENIED


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE yaqzan.audit_log SET reason_code = 'x.y'",
        "DELETE FROM yaqzan.audit_log",
        "TRUNCATE yaqzan.audit_log",
    ],
)
def test_even_the_owner_cannot_change_audit_rows(test_db: ScratchDb, statement: str) -> None:
    run(_app_executes(test_db.app_url, "SELECT 1"))
    with pytest.raises(StoreError) as caught:
        run(_app_executes(test_db.owner_url, statement))
    assert caught.value.code is StoreErrorCode.DENIED


def test_runtime_role_has_no_dangerous_attributes(test_db: ScratchDb) -> None:
    async def body() -> asyncpg.Record | None:
        conn = await asyncpg.connect(test_db.admin_url)
        try:
            return await conn.fetchrow(
                "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication, rolcanlogin"
                " FROM pg_roles WHERE rolname = 'yaqzan_app'"
            )
        finally:
            await conn.close()

    attributes = run(body())
    assert attributes is not None
    assert not any(attributes.values())


def test_app_privileges_are_exactly_what_it_needs(test_db: ScratchDb) -> None:
    """Checked from the grants themselves, not via a refused query a trigger might also refuse."""

    async def body() -> dict[str, set[str]]:
        conn = await asyncpg.connect(test_db.admin_url)
        try:
            rows = await conn.fetch(
                "SELECT table_name, privilege_type FROM information_schema.role_table_grants"
                " WHERE grantee = 'yaqzan_app' AND table_schema = 'yaqzan'"
            )
        finally:
            await conn.close()
        granted: dict[str, set[str]] = {}
        for row in rows:
            granted.setdefault(row["table_name"], set()).add(row["privilege_type"])
        return granted

    read_add = {"SELECT", "INSERT"}
    mirror = {"SELECT", "INSERT", "UPDATE"}  # vendor state changes; history is never deleted
    assert run(body()) == {
        "schema_migrations": {"SELECT"},
        "events": read_add,
        "runs": read_add,
        "findings": read_add,
        "actions": read_add,
        "audit_log": read_add,
        "sealed_secrets": {"SELECT", "INSERT", "UPDATE", "DELETE"},
        "kill_switches": {"SELECT", "INSERT", "UPDATE"},
        "revenue_subscriptions": mirror,
        "revenue_payments": mirror,
        "revenue_refunds": mirror,
        "source_syncs": mirror,
    }


def test_existing_unsafe_app_role_is_refused(test_db: ScratchDb) -> None:
    name = empty_database(test_db.server)

    async def admin(statement: str) -> None:
        conn = await asyncpg.connect(test_db.server.admin_url)
        try:
            await conn.execute(statement)
        finally:
            await conn.close()

    async def body() -> None:
        async with open_db(test_db.server.url(OWNER, test_db.server.owner_password, name)) as db:
            await migrate(db)

    run(admin("ALTER ROLE yaqzan_app BYPASSRLS"))
    try:
        with pytest.raises(StoreError) as caught:
            run(body())
        assert caught.value.code is StoreErrorCode.FAILED
    finally:
        run(admin("ALTER ROLE yaqzan_app NOBYPASSRLS"))
        drop_database(test_db.server, name)


def test_sealed_secrets_table_has_no_place_for_plaintext(test_db: ScratchDb) -> None:
    async def body() -> dict[str, str]:
        conn = await asyncpg.connect(test_db.admin_url)
        try:
            rows = await conn.fetch(
                "SELECT column_name, data_type FROM information_schema.columns"
                " WHERE table_schema = 'yaqzan' AND table_name = 'sealed_secrets'"
            )
            return {row["column_name"]: row["data_type"] for row in rows}
        finally:
            await conn.close()

    assert run(body()) == {
        "tenant_id": "uuid",
        "project_id": "uuid",
        "connector": "text",
        "name": "text",
        "environment": "text",
        "format_version": "smallint",
        "master_key_id": "text",
        "wrapped_dek": "bytea",
        "ciphertext": "bytea",
        "updated_at": "timestamp with time zone",
    }


def test_every_table_carries_a_tenant(test_db: ScratchDb) -> None:
    async def body() -> list[str]:
        conn = await asyncpg.connect(test_db.admin_url)
        try:
            rows = await conn.fetch(
                "SELECT t.table_name FROM information_schema.tables t"
                " WHERE t.table_schema = 'yaqzan' AND t.table_name <> 'schema_migrations'"
                " AND NOT EXISTS (SELECT 1 FROM information_schema.columns c WHERE c.table_schema = 'yaqzan'"
                " AND c.table_name = t.table_name AND c.column_name = 'tenant_id')"
            )
            return [row["table_name"] for row in rows]
        finally:
            await conn.close()

    assert run(body()) == []


def test_wrong_password_is_unavailable_and_leaks_nothing(test_db: ScratchDb) -> None:
    wrong = secrets.token_hex(8)
    bad = test_db.server.url("yaqzan_app_test", wrong, test_db.name)

    async def body() -> None:
        await Database.open(DatabaseSettings(dsn=SecretStr(bad), connect_timeout_s=5))

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.UNAVAILABLE
    assert wrong not in str(caught.value)


def test_nothing_listening_is_unavailable() -> None:
    url = f"postgresql://nobody:{secrets.token_hex(4)}@127.0.0.1:1/{uuid4().hex}"

    async def body() -> None:
        await Database.open(DatabaseSettings(dsn=SecretStr(url), connect_timeout_s=2))

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.UNAVAILABLE

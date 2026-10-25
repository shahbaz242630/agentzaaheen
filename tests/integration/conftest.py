"""Integration tests run against a real Postgres (Rulebook §5.1: no database mocks).

The server comes from ``AGENTZAAHEEN_TEST_DATABASE_URL`` or the running dev container
(``uv run python -m tools.dev_db up``). Once per session the schema is migrated
into a template database by a non-superuser owner; each test then gets its own
fresh copy of that template, so tests never see each other's rows.

Every tenant table sits behind a database wall (ADR-039): a project-level row
needs its workspace first (:func:`add_workspaces`), and a raw connection must
name its tenant (:func:`connect_as`).

Without a server these tests skip locally. CI sets ``AGENTZAAHEEN_REQUIRE_DB=1``,
which turns "could not run" into a failure instead of a silent pass.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator, Coroutine, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import asyncpg
import pytest
from pydantic import SecretStr

from agentzaaheen.core.store import Database, DatabaseSettings, WorkspaceRow, WorkspaceRows, migrate
from tools import dev_db

OWNER = "agentzaaheen_owner_test"
APP = "agentzaaheen_app_test"
TEMPLATE = "agentzaaheen_template"


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run one async test body to completion."""
    return asyncio.run(coro)


@dataclass(frozen=True)
class Server:
    admin_url: str
    owner_password: str
    app_password: str

    def url(self, user: str, password: str, database: str) -> str:
        parts = urlsplit(self.admin_url)
        return f"postgresql://{user}:{password}@{parts.hostname}:{parts.port}/{database}"


@dataclass(frozen=True)
class ScratchDb:
    server: Server
    name: str

    @property
    def owner_url(self) -> str:
        return self.server.url(OWNER, self.server.owner_password, self.name)

    @property
    def app_url(self) -> str:
        return self.server.url(APP, self.server.app_password, self.name)

    @property
    def admin_url(self) -> str:
        parts = urlsplit(self.server.admin_url)
        return self.server.url(parts.username or "", parts.password or "", self.name)


@asynccontextmanager
async def open_db(url: str, max_size: int = 10) -> AsyncIterator[Database]:
    db = await Database.open(DatabaseSettings(dsn=SecretStr(url), min_size=1, max_size=max_size))
    try:
        yield db
    finally:
        await db.close()


_SET_SESSION = "SELECT set_config('app.tenant_id', $1, false), set_config('app.workspace_id', $2, false)"


async def connect_as(
    url: str, tenant_id: UUID, workspace_id: UUID | None = None
) -> asyncpg.Connection[asyncpg.Record]:
    """A raw connection that names its tenant (and workspace) for its whole life, for hand-written SQL."""
    conn = await asyncpg.connect(url)
    await conn.execute(_SET_SESSION, str(tenant_id), "" if workspace_id is None else str(workspace_id))
    return conn


async def add_workspaces(url: str, tenant_id: UUID, *workspace_ids: UUID) -> None:
    """Create the tenant and the workspaces a test saves project-level rows in."""
    async with open_db(url) as db:
        rows = WorkspaceRows(db.scoped(tenant_id))
        await rows.add_tenant("Test tenant")
        for workspace_id in workspace_ids:
            slug = f"w-{workspace_id.hex}"
            await rows.add(WorkspaceRow(workspace_id=workspace_id, slug=slug, name=slug))


async def _admin(server: Server, *statements: str) -> None:
    conn = await asyncpg.connect(server.admin_url)
    try:
        for statement in statements:
            await conn.execute(statement)
    finally:
        await conn.close()


async def _ensure_role(
    conn: asyncpg.Connection[asyncpg.Record], name: str, options: str, password: str
) -> None:
    exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", name)
    verb = "ALTER" if exists else "CREATE"
    # Test-only DDL: role names are constants and the password is generated here, never user input.
    await conn.execute(f"{verb} ROLE {name} {options} PASSWORD '{password}'")


async def _build_template(server: Server) -> None:
    conn = await asyncpg.connect(server.admin_url)
    try:
        # Start from nothing: leftovers of an interrupted run, then the server-wide roles.
        for (name,) in await conn.fetch(
            "SELECT datname FROM pg_database WHERE datname ~ '^[te]_[0-9a-f]{32}$'"
        ):
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.execute(f"DROP DATABASE IF EXISTS {TEMPLATE} WITH (FORCE)")
        await conn.execute(f"DROP ROLE IF EXISTS {APP}, agentzaaheen_app")
        await _ensure_role(conn, OWNER, "LOGIN CREATEROLE NOSUPERUSER", server.owner_password)
        await conn.execute(f"CREATE DATABASE {TEMPLATE} OWNER {OWNER}")
    finally:
        await conn.close()
    async with open_db(server.url(OWNER, server.owner_password, TEMPLATE)) as db:
        await migrate(db)
    conn = await asyncpg.connect(server.admin_url)
    try:
        await _ensure_role(
            conn, APP, "LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB", server.app_password
        )
        await conn.execute(f"GRANT agentzaaheen_app TO {APP}")
    finally:
        await conn.close()


def empty_database(server: Server) -> str:
    """Create an empty database owned by the test owner; return its name."""
    name = f"e_{uuid4().hex}"
    run(_admin(server, f"CREATE DATABASE {name} OWNER {OWNER}"))
    return name


def drop_database(server: Server, name: str) -> None:
    run(_admin(server, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))


_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _server_url() -> str | None:
    url = os.environ.get("AGENTZAAHEEN_TEST_DATABASE_URL") or dev_db.admin_url()
    # Session set-up drops roles and databases: never let a mistyped URL aim that at a real server.
    if url is not None and urlsplit(url).hostname not in _LOOPBACK:
        pytest.fail("test database must be on this machine (127.0.0.1); refusing a remote server")
    return url


@pytest.fixture(scope="session")
def server() -> Server:
    url = _server_url()
    if url is None:
        message = "no test Postgres: run `uv run python -m tools.dev_db up`"
        if os.environ.get("AGENTZAAHEEN_REQUIRE_DB") == "1":
            pytest.fail(message)
        pytest.skip(message)
    found = Server(admin_url=url, owner_password=secrets.token_hex(16), app_password=secrets.token_hex(16))
    run(_build_template(found))
    return found


@pytest.fixture
def test_db(server: Server) -> Iterator[ScratchDb]:
    name = f"t_{uuid4().hex}"
    run(_admin(server, f"CREATE DATABASE {name} TEMPLATE {TEMPLATE} OWNER {OWNER}"))
    try:
        yield ScratchDb(server=server, name=name)
    finally:
        run(_admin(server, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))

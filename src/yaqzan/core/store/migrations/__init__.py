"""Versioned schema migrations, applied in order, all-or-nothing, by the owner role.

Each migration is a Python file holding SQL text (the repo is code only, ADR-014).
The runner records every applied migration with a SHA-256 checksum. If a past
migration's text has changed, or the database is ahead of this code, it refuses.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from yaqzan.core.store.db import Connection, Database
from yaqzan.core.store.errors import StoreError, StoreErrorCode
from yaqzan.core.store.migrations import (
    m0001_initial,
    m0002_revenue,
    m0003_key_expiry,
    m0004_registry_switches,
    m0005_passkeys,
    m0006_workspaces,
    m0007_agents,
    m0008_workspace_archive,
    m0009_workspace_sources,
    m0010_scout_runs,
)


@dataclass(frozen=True)
class Migration:
    """One schema step."""

    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        """SHA-256 of the SQL text, stored when the migration is applied."""
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, m0001_initial.NAME, m0001_initial.SQL),
    Migration(2, m0002_revenue.NAME, m0002_revenue.SQL),
    Migration(3, m0003_key_expiry.NAME, m0003_key_expiry.SQL),
    Migration(4, m0004_registry_switches.NAME, m0004_registry_switches.SQL),
    Migration(5, m0005_passkeys.NAME, m0005_passkeys.SQL),
    Migration(6, m0006_workspaces.NAME, m0006_workspaces.SQL),
    Migration(7, m0007_agents.NAME, m0007_agents.SQL),
    Migration(8, m0008_workspace_archive.NAME, m0008_workspace_archive.SQL),
    Migration(9, m0009_workspace_sources.NAME, m0009_workspace_sources.SQL),
    Migration(10, m0010_scout_runs.NAME, m0010_scout_runs.SQL),
)
LATEST_VERSION = MIGRATIONS[-1].version

_BOOTSTRAP = """
CREATE SCHEMA IF NOT EXISTS yaqzan;
CREATE TABLE IF NOT EXISTS yaqzan.schema_migrations (
    version    integer PRIMARY KEY,
    name       text NOT NULL,
    checksum   text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""
_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('yaqzan.migrate', 0))"
_APPLIED = "SELECT version, checksum FROM yaqzan.schema_migrations ORDER BY version"
_RECORD = "INSERT INTO yaqzan.schema_migrations (version, name, checksum) VALUES ($1, $2, $3)"


async def _applied(conn: Connection) -> dict[int, str]:
    return {row["version"]: row["checksum"] for row in await conn.fetch(_APPLIED)}


def check_history(applied: dict[int, str], migrations: tuple[Migration, ...] = MIGRATIONS) -> None:
    """Refuse if the database is ahead of this code or a past migration was edited."""
    known = {m.version: m for m in migrations}
    unknown = sorted(set(applied) - set(known))
    if unknown:
        raise StoreError(
            StoreErrorCode.SCHEMA_MISMATCH, f"database has migrations this code lacks: {unknown}"
        )
    for version, checksum in applied.items():
        if known[version].checksum != checksum:
            raise StoreError(
                StoreErrorCode.MIGRATION_CHANGED, f"migration {version} changed after it was applied"
            )


async def migrate(db: Database, migrations: tuple[Migration, ...] = MIGRATIONS) -> list[int]:
    """Apply every missing migration in one transaction; return the versions applied."""
    async with db.transaction() as conn:
        await conn.execute(_LOCK)
        await conn.execute(_BOOTSTRAP)
        applied = await _applied(conn)
        check_history(applied, migrations)
        done: list[int] = []
        for migration in migrations:
            if migration.version in applied:
                continue
            await conn.execute(migration.sql)
            await conn.execute(_RECORD, migration.version, migration.name, migration.checksum)
            done.append(migration.version)
        return done


async def check_schema(db: Database, migrations: tuple[Migration, ...] = MIGRATIONS) -> None:
    """At start-up: refuse to run unless the database is exactly at this code's schema."""
    async with db.connection() as conn:
        exists = await conn.fetchval("SELECT to_regclass('yaqzan.schema_migrations') IS NOT NULL")
        applied = await _applied(conn) if exists else {}
    check_history(applied, migrations)
    missing = sorted({m.version for m in migrations} - set(applied))
    if missing:
        raise StoreError(StoreErrorCode.SCHEMA_MISMATCH, f"database is missing migrations {missing}")

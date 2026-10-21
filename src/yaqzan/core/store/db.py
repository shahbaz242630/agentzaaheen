"""One connection pool to Postgres, and the single place database errors are translated.

Every query in the store is a fixed SQL string with ``$n`` parameters; values are
never formatted into SQL. Tables are always schema-qualified (``yaqzan.x``) so a
changed ``search_path`` cannot redirect a query. Each borrowed connection is
one client span (no SQL text, no values: ADR-032).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

import asyncpg
from asyncpg.pool import PoolConnectionProxy
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from yaqzan.core.sdk import span
from yaqzan.core.store.errors import StoreError, StoreErrorCode

type Connection = PoolConnectionProxy[asyncpg.Record]

_UNAVAILABLE_CLASSES = ("08", "28", "53", "57")  # connection, auth, resources, shutdown
_DB = {"db.system.name": "postgresql"}


class DatabaseSettings(BaseModel):
    """How to reach the database. The DSN holds a password, so it is a ``SecretStr``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dsn: SecretStr
    min_size: int = Field(default=1, ge=0, le=50)
    max_size: int = Field(default=10, ge=1, le=50)
    connect_timeout_s: float = Field(default=10.0, gt=0, le=60)
    command_timeout_s: float = Field(default=10.0, gt=0, le=300)


def json_dumps(value: object) -> str:
    """Encode JSON for a ``jsonb`` column. NaN and Infinity are refused (not valid JSON)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


async def _init_connection(conn: asyncpg.Connection[asyncpg.Record]) -> None:
    await conn.set_type_codec("jsonb", encoder=json_dumps, decoder=json.loads, schema="pg_catalog")


def require_aware(*values: datetime | None) -> None:
    """Refuse naive datetimes: a time without a zone would be stored as a guess."""
    for value in values:
        if value is not None and value.tzinfo is None:
            raise StoreError(StoreErrorCode.NAIVE_TIME, "datetimes must carry a timezone")


def translate(exc: BaseException) -> StoreError | None:  # noqa: PLR0911  # one return per error family
    """Map a driver or network error to a ``StoreError``; ``None`` if it is not a database error."""
    if isinstance(exc, StoreError):
        return exc
    if isinstance(exc, asyncpg.PostgresError):
        state = str(getattr(exc, "sqlstate", "") or "")
        if state == "23505":
            return StoreError(StoreErrorCode.CONFLICT, f"duplicate row (sqlstate {state})")
        if state.startswith(("22", "23")):  # bad value or broken rule: permanent, retrying won't help
            return StoreError(StoreErrorCode.REJECTED, f"row breaks a database rule (sqlstate {state})")
        if state == "42501":
            return StoreError(StoreErrorCode.DENIED, f"not permitted (sqlstate {state})")
        if state.startswith(_UNAVAILABLE_CLASSES):
            return StoreError(StoreErrorCode.UNAVAILABLE, f"database unavailable (sqlstate {state})")
        return StoreError(StoreErrorCode.FAILED, f"database error (sqlstate {state})")
    if isinstance(exc, asyncpg.InternalClientError):
        return StoreError(StoreErrorCode.FAILED, "database driver error")
    if isinstance(exc, (asyncpg.InterfaceError, OSError, TimeoutError)):
        return StoreError(StoreErrorCode.UNAVAILABLE, f"database unreachable ({type(exc).__name__})")
    return None


@asynccontextmanager
async def _translated() -> AsyncIterator[None]:
    try:
        yield
    except BaseException as exc:
        mapped = translate(exc)
        if mapped is None or mapped is exc:
            raise
        # ``from None``: driver messages can quote row values (e.g. a duplicate key), so they stay out.
        raise mapped from None


class Database:
    """A pool of connections. Open with :meth:`open`; always :meth:`close`."""

    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self._pool = pool

    @classmethod
    async def open(cls, settings: DatabaseSettings) -> Database:
        """Connect. Raises ``StoreError(UNAVAILABLE)`` if the database cannot be reached."""
        async with _translated():
            pool = await asyncpg.create_pool(
                dsn=settings.dsn.get_secret_value(),
                min_size=min(settings.min_size, settings.max_size),
                max_size=settings.max_size,
                timeout=settings.connect_timeout_s,
                command_timeout=settings.command_timeout_s,
                init=_init_connection,
                server_settings={"application_name": "yaqzan", "timezone": "UTC"},
            )
        return cls(pool)

    async def close(self) -> None:
        """Close every connection in the pool."""
        await self._pool.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Connection]:
        """Borrow one connection (autocommit). Database errors leave as ``StoreError``."""
        with span("db connection", kind="client", attributes=_DB):
            async with _translated(), self._pool.acquire() as conn:
                yield conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Connection]:
        """Borrow one connection inside a transaction: all of it commits, or none of it."""
        with span("db transaction", kind="client", attributes=_DB):
            async with _translated(), self._pool.acquire() as conn, conn.transaction():
                yield conn

"""Store rules that need no database: error translation, time zones, JSON and migration history."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import asyncpg
import pytest
from opentelemetry.trace import SpanKind, StatusCode

from tests.tracing import recorded
from yaqzan.core.store import MIGRATIONS, Migration, StoreError, StoreErrorCode
from yaqzan.core.store.db import Database, _translated, json_dumps, require_aware, translate
from yaqzan.core.store.migrations import check_history


def _pg_error(sqlstate: str) -> asyncpg.PostgresError:
    error_class = asyncpg.PostgresError
    for candidate in asyncpg.PostgresError.__subclasses__():
        for sub in [candidate, *candidate.__subclasses__()]:
            if getattr(sub, "sqlstate", None) == sqlstate:
                error_class = sub
    return error_class("Key (id)=(secret-value) already exists")


@pytest.mark.parametrize(
    ("sqlstate", "code"),
    [
        ("23505", StoreErrorCode.CONFLICT),
        ("23514", StoreErrorCode.REJECTED),
        ("23503", StoreErrorCode.REJECTED),
        ("22P02", StoreErrorCode.REJECTED),
        ("42501", StoreErrorCode.DENIED),
        ("28P01", StoreErrorCode.UNAVAILABLE),
        ("08006", StoreErrorCode.UNAVAILABLE),
        ("57P01", StoreErrorCode.UNAVAILABLE),
        ("53300", StoreErrorCode.UNAVAILABLE),
        ("42P01", StoreErrorCode.FAILED),
    ],
)
def test_postgres_errors_map_to_reason_codes(sqlstate: str, code: StoreErrorCode) -> None:
    mapped = translate(_pg_error(sqlstate))
    assert mapped is not None
    assert mapped.code is code
    assert "secret-value" not in str(mapped)


@pytest.mark.parametrize(
    "exc", [asyncpg.InterfaceError("pool is closed"), ConnectionRefusedError(), TimeoutError()]
)
def test_network_errors_are_unavailable(exc: BaseException) -> None:
    mapped = translate(exc)
    assert mapped is not None
    assert mapped.code is StoreErrorCode.UNAVAILABLE


def test_bad_parameter_value_is_rejected_not_unavailable() -> None:
    mapped = translate(asyncpg.DataError("invalid input for query argument $1: 'x' (expected uuid)"))
    assert mapped is not None
    assert mapped.code is StoreErrorCode.REJECTED
    assert "expected uuid" not in str(mapped)


def test_driver_internal_error_is_a_failure() -> None:
    mapped = translate(asyncpg.InternalClientError("boom"))
    assert mapped is not None
    assert mapped.code is StoreErrorCode.FAILED


def test_other_errors_are_not_ours_to_translate() -> None:
    assert translate(ValueError("x")) is None
    own = StoreError(StoreErrorCode.REJECTED, "x")
    assert translate(own) is own


def test_translation_drops_the_driver_message() -> None:
    async def body() -> None:
        async with _translated():
            raise _pg_error("23505")

    with pytest.raises(StoreError) as caught:
        asyncio.run(body())
    assert caught.value.__cause__ is None
    assert "secret-value" not in str(caught.value)


@pytest.mark.parametrize("exc", [ValueError("app bug"), StoreError(StoreErrorCode.NAIVE_TIME, "x")])
def test_non_database_errors_pass_through_unchanged(exc: Exception) -> None:
    async def body() -> None:
        async with _translated():
            raise exc

    with pytest.raises(type(exc)) as caught:
        asyncio.run(body())
    assert caught.value is exc


def test_naive_times_are_refused_aware_and_missing_are_fine() -> None:
    require_aware(datetime(2026, 10, 17, tzinfo=UTC), None)
    with pytest.raises(StoreError) as caught:
        require_aware(datetime(2026, 10, 17))  # noqa: DTZ001  # the point of the test
    assert caught.value.code is StoreErrorCode.NAIVE_TIME


def test_json_is_compact_and_refuses_nan() -> None:
    assert json_dumps({"a": [1, "é"]}) == '{"a":[1,"é"]}'
    with pytest.raises(ValueError, match="JSON"):
        json_dumps({"a": float("nan")})


def test_migration_versions_are_unique_and_ascending() -> None:
    versions = [m.version for m in MIGRATIONS]
    assert versions == sorted(set(versions))
    assert versions[0] == 1


def test_history_matching_the_code_is_accepted() -> None:
    check_history({m.version: m.checksum for m in MIGRATIONS})
    check_history({})


def test_history_with_an_unknown_version_is_refused() -> None:
    with pytest.raises(StoreError) as caught:
        check_history({999: "x"})
    assert caught.value.code is StoreErrorCode.SCHEMA_MISMATCH


def test_history_with_a_changed_checksum_is_refused() -> None:
    with pytest.raises(StoreError) as caught:
        check_history({1: Migration(1, "initial", "something else").checksum})
    assert caught.value.code is StoreErrorCode.MIGRATION_CHANGED


class _FakeConnection:
    def transaction(self) -> _Borrowed:
        return _Borrowed(None)


class _Borrowed:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakePool:
    def acquire(self) -> _Borrowed:
        return _Borrowed(_FakeConnection())


def test_each_borrowed_connection_is_one_client_span_without_sql() -> None:
    db = Database(_FakePool())  # type: ignore[arg-type]  # a stand-in pool: no database needed

    async def go() -> None:
        async with db.connection():
            pass
        async with db.transaction():
            pass

    with recorded() as spans:
        asyncio.run(go())
    for name in ("db connection", "db transaction"):
        done = spans.named(name)
        assert done.kind is SpanKind.CLIENT
        assert dict(done.attributes or {}) == {"db.system.name": "postgresql"}


def test_a_database_failure_marks_the_span_with_the_store_code() -> None:
    db = Database(_FakePool())  # type: ignore[arg-type]

    async def go() -> None:
        async with db.connection():
            raise OSError("connection reset")

    with recorded() as spans, pytest.raises(StoreError):
        asyncio.run(go())
    done = spans.named("db connection")
    assert done.status.status_code is StatusCode.ERROR
    assert (done.attributes or {})["error.type"] == "store.unavailable"

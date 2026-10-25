"""Each Claude run that did not run, against a real Postgres (F10 slice 3a, ADR-033).

Rows are added, never changed or deleted; each tenant sees only its own; a
row holds codes only, and odd values are refused by the database itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from agentzaaheen.core.store import MissRow, ModelCallMisses, StoreError, StoreErrorCode
from tests.integration.conftest import ScratchDb, add_workspaces, connect_as, open_db, run

TENANT = UUID("00000000-0000-4000-8000-0000000000f1")
OTHER = UUID("00000000-0000-4000-8000-0000000000f2")
AT = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
OPUS = "claude-opus-5-5"


def miss(at: datetime = AT, **changed: object) -> MissRow:
    fields: dict[str, object] = {
        "at": at,
        "role": "head-of-engineering",
        "use": "heads",
        "model": OPUS,
        "outcome": "model.unreachable",
        "detail": "net.auth",
        "run_id": "run-42",
    }
    return MissRow(**(fields | changed))  # type: ignore[arg-type]  # each test changes one field


@pytest.fixture(autouse=True)
def _tenants(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT))
    run(add_workspaces(test_db.app_url, OTHER))


def test_misses_come_back_oldest_first_within_the_window(test_db: ScratchDb) -> None:
    async def body() -> list[MissRow]:
        async with open_db(test_db.app_url) as db:
            rows = ModelCallMisses(db.scoped(TENANT))
            await rows.add(miss(AT + timedelta(hours=2), outcome="use_cap_reached", detail=None))
            await rows.add(miss(AT))
            await rows.add(miss(AT - timedelta(days=1)))  # before the window
            await rows.add(miss(AT + timedelta(days=1)))  # the window's end is not included
            return await rows.between(AT, AT + timedelta(days=1))

    assert run(body()) == [
        miss(AT),
        miss(AT + timedelta(hours=2), outcome="use_cap_reached", detail=None),
    ]


def test_at_most_the_limit_is_read(test_db: ScratchDb) -> None:
    async def body() -> int:
        async with open_db(test_db.app_url) as db:
            rows = ModelCallMisses(db.scoped(TENANT))
            for minute in range(5):
                await rows.add(miss(AT + timedelta(minutes=minute)))
            return len(await rows.between(AT, AT + timedelta(days=1), limit=3))

    assert run(body()) == 3


def test_each_tenant_sees_only_its_own(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await ModelCallMisses(db.scoped(TENANT)).add(miss())
            assert await ModelCallMisses(db.scoped(OTHER)).between(AT, AT + timedelta(days=1)) == []
        conn = await connect_as(test_db.app_url, OTHER)
        try:
            assert await conn.fetchval("SELECT count(*) FROM agentzaaheen.model_call_misses") == 0
            with pytest.raises(Exception, match="row-level security"):
                await conn.execute(
                    "INSERT INTO agentzaaheen.model_call_misses VALUES"
                    " ($1, gen_random_uuid(), $2, 'heads', 'heads', $3, 'model.declined', NULL, 'r1')",
                    TENANT, AT, OPUS,
                )  # fmt: skip
        finally:
            await conn.close()

    run(body())


def test_a_miss_can_never_be_changed_or_removed(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await ModelCallMisses(db.scoped(TENANT)).add(miss())
        conn = await connect_as(test_db.app_url, TENANT)
        try:
            for statement in (
                "UPDATE agentzaaheen.model_call_misses SET outcome = 'answered'",
                "DELETE FROM agentzaaheen.model_call_misses",
            ):
                with pytest.raises(Exception, match="permission denied"):
                    await conn.execute(statement)
        finally:
            await conn.close()

    run(body())


@pytest.mark.parametrize(
    "changed",
    [
        {"role": "Head Of Engineering"},  # not a role id
        {"use": "Heads; DROP"},  # not a use's name
        {"model": "Claude Opus"},  # not a model id
        {"outcome": "Fatima Al Mansoori"},  # free text where a code belongs
        {"detail": "the key sk-ant-123"},  # free text where a code belongs
        {"run_id": "run 42 with spaces"},
    ],
)
def test_anything_but_codes_is_refused(test_db: ScratchDb, changed: dict[str, object]) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            with pytest.raises(StoreError):
                await ModelCallMisses(db.scoped(TENANT)).add(miss(AT, **changed))

    run(body())


def test_a_miss_without_a_time_zone_is_refused(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            with pytest.raises(StoreError) as caught:
                await ModelCallMisses(db.scoped(TENANT)).add(miss(AT.replace(tzinfo=None)))
            assert caught.value.code is StoreErrorCode.NAIVE_TIME

    run(body())

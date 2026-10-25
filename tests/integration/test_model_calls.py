"""What each Claude call cost, against a real Postgres (F10 slice 2b, ADR-033).

The spend caps are read from the database: a month's rows added up, so a
restart or a second process sees the same amounts. Rows are added, never
changed or deleted; each tenant sees only its own; months are UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from agentzaaheen.core.llm_gateway import CAPS_USD, SpendLedger, StoreBook, Use
from agentzaaheen.core.store import ModelCalls, StoreError, StoreErrorCode
from tests.integration.conftest import ScratchDb, add_workspaces, connect_as, open_db, run

TENANT = UUID("00000000-0000-4000-8000-0000000000e1")
OTHER = UUID("00000000-0000-4000-8000-0000000000e2")
OCT = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
MONTH_START = datetime(2026, 10, 1, tzinfo=UTC)
NOV_START = datetime(2026, 11, 1, tzinfo=UTC)
OPUS = "claude-opus-5-5"


@pytest.fixture(autouse=True)
def _tenants(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT))
    run(add_workspaces(test_db.app_url, OTHER))


def test_a_months_calls_add_up_and_the_month_edges_are_utc(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            calls = ModelCalls(db.scoped(TENANT))
            await calls.add("heads", OPUS, 1_500_000, at=OCT)
            await calls.add("heads", OPUS, 250_001, at=MONTH_START)  # the first moment counts
            await calls.add("heads", OPUS, 9_000_000, at=MONTH_START - timedelta(microseconds=1))  # September
            await calls.add("heads", OPUS, 7_000_000, at=NOV_START)  # November
            await calls.add("board-chat", OPUS, 3_000_000, at=OCT)  # another use
            assert await calls.spent("heads", start=MONTH_START, end=NOV_START) == 1_750_001
            assert await calls.spent("code-review", start=MONTH_START, end=NOV_START) == 0

    run(body())


def test_the_ledger_keeps_its_caps_across_a_restart(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            first = SpendLedger(StoreBook(ModelCalls(db.scoped(TENANT))), clock=lambda: OCT)
            await first.record(Use.CODE_REVIEW, "claude-sonnet-5-5", CAPS_USD[Use.CODE_REVIEW] * 1_000_000)
        async with open_db(test_db.app_url) as db:  # a fresh process: nothing held in memory
            again = SpendLedger(StoreBook(ModelCalls(db.scoped(TENANT))), clock=lambda: OCT)
            check = await again.allows(Use.CODE_REVIEW, 1)
            assert check.refusal == "use_cap_reached"
            assert [n.cap for n in await again.notices()] == ["code-review"]
            nov = SpendLedger(StoreBook(ModelCalls(db.scoped(TENANT))), clock=lambda: NOV_START)
            assert (await nov.allows(Use.CODE_REVIEW, 1)).refusal is None  # a new month starts at zero

    run(body())


def test_each_tenant_sees_only_its_own_spend(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await ModelCalls(db.scoped(TENANT)).add("heads", OPUS, 5_000_000, at=OCT)
            other = ModelCalls(db.scoped(OTHER))
            assert await other.spent("heads", start=MONTH_START, end=NOV_START) == 0
        conn = await connect_as(test_db.app_url, OTHER)
        try:
            assert (
                await conn.fetchval("SELECT count(*) FROM agentzaaheen.model_calls") == 0
            )  # no filter at all
            with pytest.raises(Exception, match="row-level security"):
                await conn.execute(
                    "INSERT INTO agentzaaheen.model_calls VALUES ($1, gen_random_uuid(), $2, 'heads', $3, 1)",
                    TENANT, OCT, OPUS,
                )  # fmt: skip
        finally:
            await conn.close()

    run(body())


def test_a_spent_amount_can_only_go_up(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await ModelCalls(db.scoped(TENANT)).add("heads", OPUS, 5_000_000, at=OCT)
        conn = await connect_as(test_db.app_url, TENANT)
        try:
            for statement in (
                "UPDATE agentzaaheen.model_calls SET cost_micro = 0",
                "DELETE FROM agentzaaheen.model_calls",
            ):
                with pytest.raises(Exception, match="permission denied"):
                    await conn.execute(statement)
        finally:
            await conn.close()

    run(body())


@pytest.mark.parametrize(
    ("use", "model", "cost"),
    [
        ("Heads; DROP", OPUS, 1),  # not a use's name
        ("heads", "Claude Opus", 1),  # not a model id
        ("heads", OPUS, -1),  # a negative cost
    ],
)
def test_odd_rows_are_refused(test_db: ScratchDb, use: str, model: str, cost: int) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            calls = ModelCalls(db.scoped(TENANT))
            with pytest.raises((StoreError, ValueError)):
                await calls.add(use, model, cost, at=OCT)

    run(body())


def test_a_cost_without_a_time_zone_is_refused(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            with pytest.raises(StoreError) as caught:
                await ModelCalls(db.scoped(TENANT)).add("heads", OPUS, 1, at=OCT.replace(tzinfo=None))
            assert caught.value.code is StoreErrorCode.NAIVE_TIME

    run(body())

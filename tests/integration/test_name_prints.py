"""Our customers' names as keyed fingerprints only, against a real Postgres (F10 slice 2b, ADR-023).

Each workspace's prints sit behind its wall and are added, never changed; the
tenant-wide read gathers every workspace's (archived ones too) made with one
key; nothing new lands in an archived workspace; deleting a workspace for good
removes its prints. The gate then refuses a stored name, read fresh.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from agentzaaheen.core.llm_gateway import (
    MemoryBook,
    ModelGate,
    PromptPart,
    Refusal,
    SpendLedger,
    StoreNames,
    Use,
    prints_of,
)
from agentzaaheen.core.sdk import DataClass, ModelLevel
from agentzaaheen.core.store import AllNamePrints, NamePrints, StateChange, StoreError, WorkspaceLifecycle
from agentzaaheen.core.store import name_prints as name_prints_store
from tests.fixtures.name_prints import PRINTER
from tests.integration.conftest import ScratchDb, add_workspaces, connect_as, open_db, run

TENANT = UUID("00000000-0000-4000-8000-0000000000f1")
SHOP_ID = UUID("00000000-0000-4000-8000-0000000000f2")
CAFE_ID = UUID("00000000-0000-4000-8000-0000000000f3")
NOW = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
CHANGE = StateChange(by="user:chairman", at=NOW, step_up_at=NOW - timedelta(minutes=1))
ERASE = StateChange(by="user:chairman", at=NOW + timedelta(minutes=2), step_up_at=NOW + timedelta(minutes=1))


def pairs(*names: str) -> set[tuple[str, bytes]]:
    return {(p.kind.value, p.print) for p in prints_of(names, PRINTER)}


@pytest.fixture(autouse=True)
def _workspaces(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT, SHOP_ID, CAFE_ID))


def test_prints_are_gathered_across_workspaces_for_one_key_only(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            shop = NamePrints(db.scoped(TENANT, SHOP_ID))
            assert await shop.add(pairs("Fatima Al Mansoori"), key_id=PRINTER.key_id, at=NOW) == 2
            assert (
                await shop.add(pairs("Fatima Al Mansoori"), key_id=PRINTER.key_id, at=NOW) == 0
            )  # kept once
            await NamePrints(db.scoped(TENANT, CAFE_ID)).add(pairs("Grace"), key_id=PRINTER.key_id, at=NOW)
            await shop.add(pairs("Old Key Name"), key_id="names-0", at=NOW)
            every = AllNamePrints(db.scoped(TENANT))
            assert await every.made_with(PRINTER.key_id) == pairs("Fatima Al Mansoori", "Grace")
            assert await every.made_with("names-0") == pairs("Old Key Name")

    run(body())


def test_the_gate_refuses_a_stored_name_read_fresh(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            names = StoreNames(AllNamePrints(db.scoped(TENANT)), PRINTER)
            gate = ModelGate(SpendLedger(MemoryBook(), clock=lambda: NOW), names)
            part = PromptPart("Grace wrote in about her invoice.", DataClass.INTERNAL)

            async def refusal() -> Refusal | None:
                cleared = await gate.clear(
                    role="head-of-engineering",
                    role_level=ModelLevel.OPUS,
                    wanted=ModelLevel.OPUS,
                    parts=(part,),
                    max_output_tokens=100,
                    use=Use.HEADS,
                )
                return cleared.refusal

            assert await refusal() is None
            await NamePrints(db.scoped(TENANT, CAFE_ID)).add(pairs("Grace"), key_id=PRINTER.key_id, at=NOW)
            assert await refusal() is Refusal.CUSTOMER_DATA

    run(body())


def test_archived_workspaces_count_take_nothing_new_and_erasing_removes_them(
    test_db: ScratchDb,
) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            cafe = NamePrints(db.scoped(TENANT, CAFE_ID))
            await cafe.add(pairs("Grace"), key_id=PRINTER.key_id, at=NOW)
            lifecycle = WorkspaceLifecycle(db.scoped(TENANT, CAFE_ID))
            assert await lifecycle.archive(CHANGE) is not None
            every = AllNamePrints(db.scoped(TENANT))
            assert await every.made_with(PRINTER.key_id) == pairs("Grace")  # a former customer is still one
            with pytest.raises(StoreError):
                await cafe.add(pairs("Mark"), key_id=PRINTER.key_id, at=NOW)
            await lifecycle.erase(ERASE)  # its own, newer step-up: one step-up never allows two moves
            assert await every.made_with(PRINTER.key_id) == set()

    run(body())


def test_prints_are_behind_the_workspace_wall_and_never_change(test_db: ScratchDb) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await NamePrints(db.scoped(TENANT, SHOP_ID)).add(pairs("Grace"), key_id=PRINTER.key_id, at=NOW)
        conn = await connect_as(test_db.app_url, TENANT, CAFE_ID)
        try:
            assert (
                await conn.fetchval("SELECT count(*) FROM agentzaaheen.customer_name_prints") == 0
            )  # no filter
        finally:
            await conn.close()
        conn = await connect_as(test_db.app_url, TENANT, SHOP_ID)
        try:
            for statement in (
                "UPDATE agentzaaheen.customer_name_prints SET key_id = 'x'",
                "DELETE FROM agentzaaheen.customer_name_prints",
            ):
                with pytest.raises(Exception, match="permission denied"):
                    await conn.execute(statement)
        finally:
            await conn.close()

    run(body())


@pytest.mark.parametrize(
    "given",
    [
        {("full", b"short")},  # not a 32-byte print
        {("nickname", bytes(32))},  # not a kind
    ],
)
def test_odd_prints_are_refused(test_db: ScratchDb, given: set[tuple[str, bytes]]) -> None:
    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            with pytest.raises((StoreError, ValueError)):
                await NamePrints(db.scoped(TENANT, SHOP_ID)).add(given, key_id=PRINTER.key_id, at=NOW)

    run(body())


def test_too_many_prints_at_once_are_refused_before_the_database(
    test_db: ScratchDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(name_prints_store, "MAX_PRINTS", 2)

    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            with pytest.raises(ValueError, match="at most 2"):
                await NamePrints(db.scoped(TENANT, SHOP_ID)).add(
                    pairs("Fatima Al Mansoori", "Grace"), key_id="k", at=NOW
                )

    run(body())

"""The revenue mirror against real Postgres: upserts, newest-wins, atomic saves, tenant scoping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from tests.fixtures import paddle_world as world
from tests.integration.conftest import ScratchDb, add_workspaces, open_db, run
from yaqzan.core.sdk import Payment, PaymentStatus, RevenueBatch, SubscriptionStatus
from yaqzan.core.store import RevenueRows, StoreError, StoreErrorCode, SyncState
from yaqzan.core.store import revenue as revenue_module

TENANT = UUID("00000000-0000-4000-8000-000000000001")
PROJECT = UUID("00000000-0000-4000-8000-0000000000aa")


@pytest.fixture(autouse=True)
def _workspace(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT, PROJECT))


START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 11, 1, tzinfo=UTC)
AT = world.NOW


def full_batch(**changes: object) -> RevenueBatch:
    base = {
        "source": "paddle",
        "subscriptions": world.EXPECTED_SUBSCRIPTIONS,
        "payments": world.EXPECTED_PAYMENTS,
        "refunds": world.EXPECTED_REFUNDS,
        "fetched_at": AT,
    }
    return RevenueBatch.model_validate({**base, **changes})


def test_a_saved_batch_reads_back_exactly(test_db: ScratchDb) -> None:
    async def body() -> tuple[object, ...]:
        async with open_db(test_db.app_url) as db:
            rows = RevenueRows(db.scoped(TENANT, PROJECT))
            await rows.save_batch(full_batch(), at=AT)
            return (
                await rows.subscriptions("paddle"),
                await rows.payments("paddle", START, END),
                await rows.refunds("paddle", START, END),
                await rows.sync_state("paddle"),
            )

    subs, pays, refunds, state = run(body())
    assert set(subs) == set(world.EXPECTED_SUBSCRIPTIONS)  # type: ignore[call-overload]
    assert set(pays) == set(world.EXPECTED_PAYMENTS)  # type: ignore[call-overload]
    assert set(refunds) == set(world.EXPECTED_REFUNDS)  # type: ignore[call-overload]
    assert state == SyncState(last_attempt_at=AT, last_success_at=AT, last_error_code=None, cursor_at=AT)


def test_saving_twice_changes_nothing(test_db: ScratchDb) -> None:
    async def body() -> int:
        async with open_db(test_db.app_url) as db:
            rows = RevenueRows(db.scoped(TENANT, PROJECT))
            await rows.save_batch(full_batch(), at=AT)
            await rows.save_batch(full_batch(), at=AT + timedelta(minutes=5))
            return len(await rows.payments("paddle", START, END))

    assert run(body()) == len(world.EXPECTED_PAYMENTS)


def test_older_vendor_data_never_overwrites_newer(test_db: ScratchDb) -> None:
    first = world.EXPECTED_SUBSCRIPTIONS[0]
    newer = first.model_copy(
        update={
            "status": SubscriptionStatus.CANCELED,
            "vendor_updated_at": first.vendor_updated_at + timedelta(days=1),
        }
    )
    older = first.model_copy(
        update={
            "status": SubscriptionStatus.PAST_DUE,
            "vendor_updated_at": first.vendor_updated_at - timedelta(days=1),
        }
    )

    async def body() -> SubscriptionStatus:
        async with open_db(test_db.app_url) as db:
            rows = RevenueRows(db.scoped(TENANT, PROJECT))
            await rows.save_batch(full_batch(subscriptions=(newer,), payments=(), refunds=()), at=AT)
            await rows.save_batch(full_batch(subscriptions=(older,), payments=(), refunds=()), at=AT)
            (stored,) = await rows.subscriptions("paddle")
            return stored.status

    assert run(body()) is SubscriptionStatus.CANCELED


def test_a_payment_attempt_takes_the_vendors_latest_status(test_db: ScratchDb) -> None:
    pending = world.EXPECTED_PAYMENTS[1].model_copy(update={"status": PaymentStatus.PENDING})

    async def body() -> PaymentStatus:
        async with open_db(test_db.app_url) as db:
            rows = RevenueRows(db.scoped(TENANT, PROJECT))
            await rows.save_batch(full_batch(subscriptions=(), payments=(pending,), refunds=()), at=AT)
            await rows.save_batch(
                full_batch(subscriptions=(), refunds=(), payments=(world.EXPECTED_PAYMENTS[1],)),
                at=AT,
            )
            (stored,) = await rows.payments("paddle", START, END)
            return stored.status

    assert run(body()) is PaymentStatus.SUCCEEDED


def test_a_rejected_row_saves_nothing_and_leaves_the_sync_unmarked(test_db: ScratchDb) -> None:
    # Bypass the SDK's own checks to prove the database rule holds on its own.
    bad = Payment.model_construct(**{**world.EXPECTED_PAYMENTS[0].model_dump(), "currency": "usd"})
    batch = RevenueBatch.model_construct(
        source="paddle",
        subscriptions=world.EXPECTED_SUBSCRIPTIONS,
        payments=(bad,),
        refunds=(),
        fetched_at=AT,
    )

    async def body() -> tuple[int, SyncState | None]:
        async with open_db(test_db.app_url) as db:
            rows = RevenueRows(db.scoped(TENANT, PROJECT))
            with pytest.raises(StoreError) as caught:
                await rows.save_batch(batch, at=AT)
            assert caught.value.code is StoreErrorCode.REJECTED
            return len(await rows.subscriptions("paddle")), await rows.sync_state("paddle")

    assert run(body()) == (0, None)


def test_rows_are_scoped_to_tenant_project_and_source(test_db: ScratchDb) -> None:
    async def body() -> list[int]:
        async with open_db(test_db.app_url) as db:
            rows = RevenueRows(db.scoped(TENANT, PROJECT))
            await rows.save_batch(full_batch(), at=AT)
            other_tenant = RevenueRows(db.scoped(uuid4(), PROJECT))
            other_project = RevenueRows(db.scoped(TENANT, uuid4()))
            return [
                len(await other_tenant.subscriptions("paddle")),
                len(await other_project.subscriptions("paddle")),
                len(await rows.subscriptions("stripe")),
                len(await other_tenant.payments("paddle", START, END)),
                len(await other_project.refunds("paddle", START, END)),
            ]

    assert run(body()) == [0, 0, 0, 0, 0]


def test_windows_include_start_and_exclude_end(test_db: ScratchDb) -> None:
    async def body() -> tuple[int, int]:
        async with open_db(test_db.app_url) as db:
            rows = RevenueRows(db.scoped(TENANT, PROJECT))
            await rows.save_batch(full_batch(), at=AT)
            day = world.when(5)  # the annual payment happened exactly at this moment
            return (
                len(await rows.payments("paddle", day, day + timedelta(seconds=1))),
                len(await rows.payments("paddle", day - timedelta(days=1), day)),
            )

    assert run(body()) == (1, 0)


def test_failure_keeps_the_last_success(test_db: ScratchDb) -> None:
    later = AT + timedelta(hours=1)

    async def body() -> SyncState | None:
        async with open_db(test_db.app_url) as db:
            rows = RevenueRows(db.scoped(TENANT, PROJECT))
            await rows.save_batch(full_batch(), at=AT)
            await rows.mark_attempt("paddle", at=later)
            await rows.mark_failure("paddle", at=later, error_code="source.auth")
            return await rows.sync_state("paddle")

    assert run(body()) == SyncState(
        last_attempt_at=later, last_success_at=AT, last_error_code="source.auth", cursor_at=AT
    )


def test_naive_times_are_refused(test_db: ScratchDb) -> None:
    naive = datetime(2026, 10, 18)  # noqa: DTZ001  # the point of the test

    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await RevenueRows(db.scoped(TENANT, PROJECT)).mark_attempt("paddle", at=naive)

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.NAIVE_TIME


def test_a_read_over_the_row_cap_is_refused_not_cut_short(
    test_db: ScratchDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(revenue_module, "MAX_ROWS", 2)

    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            rows = RevenueRows(db.scoped(TENANT, PROJECT))
            await rows.save_batch(full_batch(), at=AT)
            await rows.subscriptions("paddle")

    with pytest.raises(StoreError) as caught:
        run(body())
    assert caught.value.code is StoreErrorCode.FAILED

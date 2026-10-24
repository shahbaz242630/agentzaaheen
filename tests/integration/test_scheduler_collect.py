"""One revenue sync end to end, and the snapshot that turns stale data into "unknown", never 0."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from tests.fixtures import paddle_world as world
from tests.integration.conftest import ScratchDb, add_workspaces, open_db, run
from tests.tracing import recorded
from yaqzan.core.metrics import CountAndAmounts, Freshness, Unknown
from yaqzan.core.net import JsonClient, NetPolicy
from yaqzan.core.scheduler import CURSOR_OVERLAP, RevenueSnapshot, SyncOutcome, revenue_snapshot, sync_revenue
from yaqzan.core.sdk import RevenueBatch, SourceErrorCode
from yaqzan.core.store import Database, RevenueRows, SyncState
from yaqzan.plugins.connectors.paddle import (
    ALLOWED_HOSTS,
    API_VERSION_HEADER,
    SANDBOX_BASE_URL,
    FakePaddleSource,
    PaddleSource,
)

TENANT = UUID("00000000-0000-4000-8000-000000000001")
PROJECT = UUID("00000000-0000-4000-8000-0000000000aa")


@pytest.fixture(autouse=True)
def _workspace(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT, PROJECT))


SCHEDULE = timedelta(minutes=15)
START, END = world.when(1, 0), world.when(18, 12)


def fake() -> FakePaddleSource:
    return FakePaddleSource(world.EXPECTED_SUBSCRIPTIONS, world.EXPECTED_PAYMENTS, world.EXPECTED_REFUNDS)


async def snapshot(db: Database, now: datetime) -> RevenueSnapshot:
    return await revenue_snapshot(
        db.scoped(TENANT, PROJECT), "paddle", now=now, schedule=SCHEDULE, start=START, end=END
    )


def assert_world_numbers(snap: RevenueSnapshot) -> None:
    assert snap.freshness is Freshness.FRESH
    assert snap.mrr == {"GBP": 300, "USD": 1300}
    assert snap.active_subscribers == 3
    assert snap.trials == 1
    assert snap.failed_payments == CountAndAmounts(2, {"USD": 945})
    assert snap.refunds == CountAndAmounts(2, {"USD": 5040})
    assert snap.cash_revenue == {"GBP": 360, "USD": 525}
    assert snap.cash_revenue_ex_tax == {"GBP": 300, "USD": 500}


def test_sync_then_snapshot_gives_the_metric_numbers(test_db: ScratchDb) -> None:
    async def body() -> tuple[SyncOutcome, RevenueSnapshot]:
        async with open_db(test_db.app_url) as db:
            outcome = await sync_revenue(db.scoped(TENANT, PROJECT), fake(), now=world.NOW)
            return outcome, await snapshot(db, world.NOW + timedelta(minutes=5))

    outcome, snap = run(body())
    assert outcome == SyncOutcome(True, None, 7, 5, 4)
    assert snap.last_success_at == world.NOW
    assert_world_numbers(snap)


def test_the_real_paddle_source_through_the_real_client_gives_the_same_numbers(test_db: ScratchDb) -> None:
    paddle = world.PaddleWorld()

    async def body() -> RevenueSnapshot:
        http = JsonClient(
            SANDBOX_BASE_URL,
            policy=NetPolicy(allowed_hosts=ALLOWED_HOSTS),
            headers=API_VERSION_HEADER,
            secret_headers={"Authorization": SecretStr("Bearer t")},
            transport=httpx.MockTransport(paddle.serve),
        )
        async with open_db(test_db.app_url) as db, http:
            outcome = await sync_revenue(db.scoped(TENANT, PROJECT), PaddleSource(http), now=world.NOW)
            assert outcome.ok
            return await snapshot(db, world.NOW)

    assert_world_numbers(run(body()))


def test_the_next_sync_asks_only_from_just_before_the_cursor(test_db: ScratchDb) -> None:
    source = fake()

    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await sync_revenue(db.scoped(TENANT, PROJECT), source, now=world.NOW)
            await sync_revenue(db.scoped(TENANT, PROJECT), source, now=world.NOW + SCHEDULE)

    run(body())
    assert source.calls == [None, world.NOW - CURSOR_OVERLAP]


def test_a_failed_sync_records_why_and_keeps_the_last_success(test_db: ScratchDb) -> None:
    source = fake()
    later = world.NOW + SCHEDULE

    async def body() -> tuple[SyncOutcome, SyncState | None, RevenueSnapshot]:
        async with open_db(test_db.app_url) as db:
            await sync_revenue(db.scoped(TENANT, PROJECT), source, now=world.NOW)
            source.fail_with = SourceErrorCode.AUTH
            outcome = await sync_revenue(db.scoped(TENANT, PROJECT), source, now=later)
            state = await RevenueRows(db.scoped(TENANT, PROJECT)).sync_state("paddle")
            return outcome, state, await snapshot(db, later)

    outcome, state, snap = run(body())
    assert outcome == SyncOutcome(ok=False, error_code="source.auth")
    assert state == SyncState(
        last_attempt_at=later, last_success_at=world.NOW, last_error_code="source.auth", cursor_at=world.NOW
    )
    assert_world_numbers(snap)  # one missed sync is still within 2x the schedule


def test_a_failure_is_logged_in_plain_english_with_the_precise_code(
    test_db: ScratchDb, capsys: pytest.CaptureFixture[str]
) -> None:
    key = "pdl_sdbx_apikey_" + "z" * 20  # built here, never a real key

    async def body() -> tuple[SyncOutcome, SyncState | None]:
        http = JsonClient(
            SANDBOX_BASE_URL,
            policy=NetPolicy(allowed_hosts=ALLOWED_HOSTS),
            secret_headers={"Authorization": SecretStr(f"Bearer {key}")},
            transport=httpx.MockTransport(lambda r: httpx.Response(401)),
        )
        async with open_db(test_db.app_url) as db, http:
            outcome = await sync_revenue(db.scoped(TENANT, PROJECT), PaddleSource(http), now=world.NOW)
            return outcome, await RevenueRows(db.scoped(TENANT, PROJECT)).sync_state("paddle")

    outcome, state = run(body())
    assert outcome == SyncOutcome(ok=False, error_code="net.auth")
    assert state is not None
    assert state.last_error_code == "net.auth"
    logged = capsys.readouterr().err
    line = next(json.loads(x) for x in logged.splitlines() if "revenue_sync_failed" in x)
    assert line["stage"] == "fetch from paddle"
    assert line["operation"] == "GET /subscriptions"
    assert line["attempt"] == "1/4"
    assert line["http_status"] == 401
    assert line["reason_code"] == "net.auth"
    assert (
        line["what"]
        == "The revenue sync stopped while trying to fetch from paddle: Paddle refused our API key."
    )
    assert line["next"].startswith("Create a new read-only key in Paddle's settings")
    assert key not in logged


def test_stale_data_is_unknown_never_zero(test_db: ScratchDb) -> None:
    async def body() -> tuple[RevenueSnapshot, RevenueSnapshot]:
        async with open_db(test_db.app_url) as db:
            await sync_revenue(db.scoped(TENANT, PROJECT), fake(), now=world.NOW)
            stale = await snapshot(db, world.NOW + 2 * SCHEDULE + timedelta(seconds=1))
            alert = await snapshot(db, world.NOW + 6 * SCHEDULE + timedelta(seconds=1))
            return stale, alert

    stale, alert = run(body())
    assert stale.freshness is Freshness.STALE
    assert alert.freshness is Freshness.ALERT
    for snap, reason in ((stale, "freshness.stale"), (alert, "freshness.alert")):
        numbers = (
            snap.mrr,
            snap.active_subscribers,
            snap.trials,
            snap.failed_payments,
            snap.refunds,
            snap.cash_revenue,
            snap.cash_revenue_ex_tax,
        )
        assert all(n == Unknown(reason) for n in numbers)


def test_a_source_that_never_synced_is_unknown(test_db: ScratchDb) -> None:
    async def body() -> RevenueSnapshot:
        async with open_db(test_db.app_url) as db:
            return await snapshot(db, world.NOW)

    snap = run(body())
    assert snap.freshness is Freshness.NEVER
    assert snap.mrr == Unknown("freshness.never")
    assert snap.last_success_at is None


class WrongSource(FakePaddleSource):
    async def fetch(self, *, payments_since: datetime | None, now: datetime) -> RevenueBatch:
        return RevenueBatch(source="stripe", fetched_at=now)


class Crashing(FakePaddleSource):
    async def fetch(self, *, payments_since: datetime | None, now: datetime) -> RevenueBatch:
        raise RuntimeError("a bug")


def test_a_batch_from_another_source_is_refused(test_db: ScratchDb) -> None:
    async def body() -> SyncOutcome:
        async with open_db(test_db.app_url) as db:
            return await sync_revenue(db.scoped(TENANT, PROJECT), WrongSource(), now=world.NOW)

    assert run(body()) == SyncOutcome(ok=False, error_code="source.bad_response")


def test_a_crash_is_recorded_then_raised(test_db: ScratchDb) -> None:
    async def body() -> SyncState | None:
        async with open_db(test_db.app_url) as db:
            with pytest.raises(RuntimeError):
                await sync_revenue(db.scoped(TENANT, PROJECT), Crashing(), now=world.NOW)
            return await RevenueRows(db.scoped(TENANT, PROJECT)).sync_state("paddle")

    state = run(body())
    assert state is not None
    assert state.last_error_code == "collect.crashed"
    assert state.last_success_at is None


def test_each_sync_is_one_span_with_its_count_or_its_failure(test_db: ScratchDb) -> None:
    source = fake()

    async def body() -> None:
        async with open_db(test_db.app_url) as db:
            await sync_revenue(db.scoped(TENANT, PROJECT), source, now=world.NOW)
            source.fail_with = SourceErrorCode.AUTH
            await sync_revenue(db.scoped(TENANT, PROJECT), source, now=world.NOW + SCHEDULE)

    with recorded() as spans:
        run(body())
    good, bad = [s for s in spans.spans if s.name == "sync revenue paddle"]
    expected = len(world.EXPECTED_SUBSCRIPTIONS) + len(world.EXPECTED_PAYMENTS) + len(world.EXPECTED_REFUNDS)
    assert dict(good.attributes or {}) == {
        "yaqzan.source": "paddle",
        "yaqzan.project": str(PROJECT),
        "yaqzan.count": expected,
    }
    assert (bad.attributes or {})["error.type"] == "source.auth"
    assert good.context is not None
    db_spans = [s for s in spans.spans if s.name.startswith("db ") and s.parent is not None]
    assert any(s.parent.span_id == good.context.span_id for s in db_spans if s.parent is not None)

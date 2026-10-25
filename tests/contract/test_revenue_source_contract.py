"""One suite, two implementations: the fake Paddle source and the real one (Rulebook §2.3, §5.4).

The real ``PaddleSource`` runs through the core's real ``JsonClient`` against
``PaddleWorld`` (a pretend Paddle on ``httpx.MockTransport``), so paging, the
host allowlist and the key header are all exercised. The fake is loaded with
what that world must map to. Both must behave the same.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from agentzaaheen.core.net import JsonClient, NetPolicy
from agentzaaheen.core.sdk import RevenueBatch, RevenueSource, SourceError, SourceErrorCode
from agentzaaheen.plugins.connectors.paddle import (
    ALLOWED_HOSTS,
    API_VERSION_HEADER,
    SANDBOX_BASE_URL,
    FakePaddleSource,
    PaddleSource,
)
from tests.fixtures import paddle_world as world

FAKE_KEY = "pdl_sdbx_apikey_" + "z" * 20  # built here, never a real key


def real_source(paddle: world.PaddleWorld) -> tuple[RevenueSource, JsonClient]:
    http = JsonClient(
        SANDBOX_BASE_URL,
        policy=NetPolicy(allowed_hosts=ALLOWED_HOSTS),
        headers=API_VERSION_HEADER,
        secret_headers={"Authorization": SecretStr(f"Bearer {FAKE_KEY}")},
        transport=httpx.MockTransport(paddle.serve),
    )
    return PaddleSource(http), http


def fake_source() -> FakePaddleSource:
    return FakePaddleSource(world.EXPECTED_SUBSCRIPTIONS, world.EXPECTED_PAYMENTS, world.EXPECTED_REFUNDS)


Fetch = Callable[[datetime | None], RevenueBatch]


def _real_fetch(since: datetime | None) -> RevenueBatch:
    async def body() -> RevenueBatch:
        source, http = real_source(world.PaddleWorld())
        async with http:
            return await source.fetch(payments_since=since, now=world.NOW)

    return asyncio.run(body())


def _fake_fetch(since: datetime | None) -> RevenueBatch:
    return asyncio.run(fake_source().fetch(payments_since=since, now=world.NOW))


@pytest.fixture(params=["real", "fake"])
def fetch(request: pytest.FixtureRequest) -> Fetch:
    return _real_fetch if request.param == "real" else _fake_fetch


# ---------------------------------------------------------------- the shared contract


def test_full_fetch_returns_every_shape(fetch: Fetch) -> None:
    batch = fetch(None)
    assert batch.source == "paddle"
    assert batch.fetched_at == world.NOW
    assert set(batch.subscriptions) == set(world.EXPECTED_SUBSCRIPTIONS)
    assert set(batch.payments) == set(world.EXPECTED_PAYMENTS)
    assert set(batch.refunds) == set(world.EXPECTED_REFUNDS)


def test_payments_since_includes_every_later_payment(fetch: Fetch) -> None:
    since = world.when(11)
    batch = fetch(since)
    later = {p for p in world.EXPECTED_PAYMENTS if p.occurred_at >= since}
    assert later <= set(batch.payments)
    assert set(batch.subscriptions) == set(world.EXPECTED_SUBSCRIPTIONS)


def test_fetching_twice_gives_the_same_answer(fetch: Fetch) -> None:
    assert fetch(None) == fetch(None)


def test_no_personal_data_survives_mapping(fetch: Fetch) -> None:
    dumped = fetch(None).model_dump_json()
    for never in ("never-read", "Never Read", "4242", "free text", "SPRING20", "example.invalid"):
        assert never not in dumped


# ---------------------------------------------------------------- the real adapter only


def test_real_source_pages_through_everything_with_get_only() -> None:
    paddle = world.PaddleWorld()

    async def body() -> None:
        source, http = real_source(paddle)
        async with http:
            await source.fetch(payments_since=None, now=world.NOW)

    asyncio.run(body())
    assert {r.method for r in paddle.requests} == {"GET"}
    assert {r.url.host for r in paddle.requests} == {"sandbox-api.paddle.com"}
    assert {r.url.path for r in paddle.requests} == {
        "/subscriptions",
        "/transactions",
        "/adjustments",
        "/discounts",
    }
    subscription_pages = [r for r in paddle.requests if r.url.path == "/subscriptions"]
    assert len(subscription_pages) == 4  # 7 subscriptions, 2 per page
    assert all(r.headers["Authorization"] == f"Bearer {FAKE_KEY}" for r in paddle.requests)
    assert all(r.headers["Paddle-Version"] == "1" for r in paddle.requests)


def test_real_source_asks_paddle_only_for_changed_transactions() -> None:
    paddle = world.PaddleWorld()
    since = world.when(11, 10)

    async def body() -> RevenueBatch:
        source, http = real_source(paddle)
        async with http:
            return await source.fetch(payments_since=since, now=world.NOW)

    batch = asyncio.run(body())
    txn_requests = [r for r in paddle.requests if r.url.path == "/transactions"]
    assert all(r.url.params["updated_at[GTE]"] == "2026-10-11T10:00:00.000000Z" for r in txn_requests)
    assert {p.transaction_ref for p in batch.payments} == {world.pid("txn", n) for n in (1, 3, 5)}


def test_real_source_stops_at_the_page_cap() -> None:
    async def body() -> None:
        _, http = real_source(world.PaddleWorld())
        async with http:
            await PaddleSource(http, max_pages=2).fetch(payments_since=None, now=world.NOW)

    with pytest.raises(SourceError) as caught:
        asyncio.run(body())
    assert caught.value.code is SourceErrorCode.TOO_MUCH_DATA
    assert caught.value.operation == "GET /subscriptions"


@pytest.mark.parametrize(
    "meta",
    [
        {"pagination": {"has_more": True, "next": None}},
        {"pagination": {"has_more": True, "next": "https://sandbox-api.paddle.com/subscriptions?per_page=2"}},
        {"pagination": "nope"},
        None,
    ],
)
def test_broken_pagination_is_a_bad_response(meta: object) -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "meta": meta})

    async def body() -> None:
        http = JsonClient(
            SANDBOX_BASE_URL,
            policy=NetPolicy(allowed_hosts=ALLOWED_HOSTS),
            transport=httpx.MockTransport(serve),
        )
        async with http:
            await PaddleSource(http).fetch(payments_since=None, now=world.NOW)

    with pytest.raises(SourceError) as caught:
        asyncio.run(body())
    assert caught.value.code is SourceErrorCode.BAD_RESPONSE


def test_a_refused_key_surfaces_as_auth() -> None:
    async def body() -> None:
        http = JsonClient(
            SANDBOX_BASE_URL,
            policy=NetPolicy(allowed_hosts=ALLOWED_HOSTS),
            transport=httpx.MockTransport(lambda r: httpx.Response(403)),
        )
        async with http:
            await PaddleSource(http).fetch(payments_since=None, now=world.NOW)

    with pytest.raises(SourceError) as caught:
        asyncio.run(body())
    assert caught.value.code is SourceErrorCode.AUTH
    assert caught.value.reason_code == "net.auth"
    where = (caught.value.operation, caught.value.attempt, caught.value.attempts, caught.value.status)
    assert where == ("GET /subscriptions", 1, 4, 403)  # the client's detail, not overwritten by the adapter
    assert FAKE_KEY not in str(caught.value)


def test_data_breaking_a_shape_rule_is_a_bad_response() -> None:
    broken = [
        world.txn(
            9,
            "completed",
            1,
            1,
            [world.attempt(9, "captured", "10", world.at(3))],
            tax="99",
            total="10",
            updated=world.at(3),
        )
    ]
    broken[0]["payments"] = [
        dict(world.attempt(9, "captured", "10", world.at(3)), payment_attempt_id="bad id!")
    ]

    async def body() -> None:
        source, http = real_source(world.PaddleWorld(transactions=broken))
        async with http:
            await source.fetch(payments_since=None, now=world.NOW)

    with pytest.raises(SourceError) as caught:
        asyncio.run(body())
    assert caught.value.code is SourceErrorCode.BAD_RESPONSE
    assert caught.value.operation == "read paddle data"


def test_an_unseen_value_says_it_failed_reading_paddle_data() -> None:
    odd = [dict(world.SUBSCRIPTIONS[0], status="hibernating")]

    async def body() -> None:
        source, http = real_source(world.PaddleWorld(subscriptions=odd))
        async with http:
            await source.fetch(payments_since=None, now=world.NOW)

    with pytest.raises(SourceError) as caught:
        asyncio.run(body())
    assert caught.value.code is SourceErrorCode.UNKNOWN_VALUE
    assert caught.value.operation == "read paddle data"


# ---------------------------------------------------------------- the fake only


def test_fake_can_be_told_to_fail_and_records_calls() -> None:
    fake = fake_source()
    fake.fail_with = SourceErrorCode.UNAVAILABLE
    since = world.NOW - timedelta(days=1)
    with pytest.raises(SourceError) as caught:
        asyncio.run(fake.fetch(payments_since=since, now=world.NOW))
    assert caught.value.code is SourceErrorCode.UNAVAILABLE
    assert fake.calls == [since]
    assert fake.source == "paddle"


def test_customer_names_are_read_only_from_customers_and_never_with_revenue() -> None:
    paddle = world.PaddleWorld()

    async def body() -> list[str]:
        http = JsonClient(
            SANDBOX_BASE_URL,
            policy=NetPolicy(allowed_hosts=ALLOWED_HOSTS),
            headers=API_VERSION_HEADER,
            secret_headers={"Authorization": SecretStr(f"Bearer {FAKE_KEY}")},
            transport=httpx.MockTransport(paddle.serve),
        )
        async with http:
            return await PaddleSource(http).customer_names()

    names = asyncio.run(body())
    assert sorted(names) == [
        "Fatima Al Mansoori",
        "Grace",
        "Zoë O'Brien",
    ]  # no name: skipped
    assert {(r.method, r.url.path) for r in paddle.requests} == {("GET", "/customers")}
    assert len(paddle.requests) == 2  # 4 customers, 2 per page

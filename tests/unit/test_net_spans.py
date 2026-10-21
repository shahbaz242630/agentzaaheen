"""Every outside call is one client span: host, status, attempts and retries; never a path, query or key.

No real network: every request goes to an ``httpx.MockTransport`` handler.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator

import httpx
import pytest
from opentelemetry.trace import SpanKind, StatusCode
from pydantic import SecretStr

from tests.tracing import Recorded, recorded
from yaqzan.core.net import (
    DownloadPolicy,
    JsonClient,
    ModelClient,
    NetError,
    NetPolicy,
    PushClient,
    StatusProbe,
)

HOST = "api.example.com"
POLICY = NetPolicy(allowed_hosts=frozenset({HOST}), max_attempts=3)
FAKE_KEY = "pdl_" + "k" * 24
TOPIC = "topic" + "x" * 20


async def no_sleep(_delay: float) -> None:
    return None


@pytest.fixture
def spans() -> Iterator[Recorded]:
    with recorded() as kept:
        yield kept


def json_client(handler: httpx.MockTransport) -> JsonClient:
    return JsonClient(
        f"https://{HOST}",
        policy=POLICY,
        secret_headers={"Authorization": SecretStr(f"Bearer {FAKE_KEY}")},
        transport=handler,
        sleep=no_sleep,
        jitter=lambda: 0.0,
        download=DownloadPolicy(host_pattern=re.compile(r"files\.example\.com")),
    )


def everything(spans: Recorded) -> str:
    return " ".join(s.to_json() for s in spans.spans)


def test_a_get_is_one_client_span_named_by_host(spans: Recorded) -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, json={"ok": True}))

    async def go() -> None:
        async with json_client(transport) as client:
            await client.get_json("/repos/secret-repo/pulls", {"state": "open"})

    asyncio.run(go())
    done = spans.named(f"GET {HOST}")
    assert done.kind is SpanKind.CLIENT
    assert dict(done.attributes or {}) == {
        "server.address": HOST,
        "http.request.method": "GET",
        "yaqzan.attempts": 1,
        "http.response.status_code": 200,
    }
    text = everything(spans)
    assert "secret-repo" not in text
    assert "pulls" not in text
    assert FAKE_KEY not in text


def test_retries_are_events_on_the_one_span(spans: Recorded) -> None:
    answers = iter([httpx.Response(503), httpx.Response(200, json={})])
    transport = httpx.MockTransport(lambda _r: next(answers))

    async def go() -> None:
        async with json_client(transport) as client:
            await client.get_json("/x", {})

    asyncio.run(go())
    done = spans.named(f"GET {HOST}")
    assert (done.attributes or {})["yaqzan.attempts"] == 2
    assert [(e.name, dict(e.attributes or {})) for e in done.events] == [
        ("retry", {"yaqzan.attempt": 1, "yaqzan.reason": "http_503"})
    ]


def test_a_failed_get_carries_the_code_status_and_attempts(spans: Recorded) -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(401))

    async def go() -> None:
        async with json_client(transport) as client:
            await client.get_json("/x", {})

    with pytest.raises(NetError):
        asyncio.run(go())
    done = spans.named(f"GET {HOST}")
    assert done.status.status_code is StatusCode.ERROR
    attributes = dict(done.attributes or {})
    assert attributes["error.type"] == "net.auth"
    assert attributes["http.response.status_code"] == 401
    assert attributes["yaqzan.attempts"] == 1


def test_a_refusal_before_sending_has_no_status(spans: Recorded) -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, json={}))

    async def go() -> None:
        async with json_client(transport) as client:
            await client.get_json("/../etc", {})

    with pytest.raises(NetError):
        asyncio.run(go())
    attributes = dict(spans.named(f"GET {HOST}").attributes or {})
    assert attributes["error.type"] == "net.blocked"
    assert "http.response.status_code" not in attributes


def test_a_download_is_its_own_span(spans: Recorded) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == HOST:
            return httpx.Response(302, headers={"Location": "https://files.example.com/log?sig=abc"})
        return httpx.Response(200, content=b"log text")

    async def go() -> bytes:
        async with json_client(httpx.MockTransport(handler)) as client:
            return await client.get_redirected_bytes("/logs/1", {})

    assert asyncio.run(go()) == b"log text"
    done = spans.named(f"GET {HOST} (download)")
    assert done.status.status_code is StatusCode.UNSET
    assert "sig=abc" not in everything(spans)


def test_a_probe_span_has_the_status_and_time(spans: Recorded) -> None:
    clock = iter([1.0, 1.25])

    async def go() -> None:
        transport = httpx.MockTransport(lambda _r: httpx.Response(204))
        async with StatusProbe(frozenset({HOST}), transport=transport, clock=lambda: next(clock)) as probe:
            await probe.status_of(f"https://{HOST}/health")

    asyncio.run(go())
    done = spans.named(f"probe {HOST}")
    assert dict(done.attributes or {}) == {
        "server.address": HOST,
        "http.request.method": "GET",
        "http.response.status_code": 204,
        "yaqzan.elapsed_ms": 250,
    }


def test_a_probe_that_times_out_is_a_failed_span(spans: Recorded) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    async def go() -> None:
        async with StatusProbe(frozenset({HOST}), transport=httpx.MockTransport(handler)) as probe:
            await probe.status_of(f"https://{HOST}/")

    with pytest.raises(NetError):
        asyncio.run(go())
    assert (spans.named(f"probe {HOST}").attributes or {})["error.type"] == "net.timeout"


@pytest.mark.parametrize(("status", "failed"), [(200, False), (500, True)])
def test_a_push_span_never_holds_the_topic(spans: Recorded, status: int, failed: bool) -> None:
    async def go() -> None:
        transport = httpx.MockTransport(lambda _r: httpx.Response(status))
        async with PushClient(SecretStr(TOPIC), transport=transport) as pusher:
            await pusher.send("Eva is down", "1 of 2 addresses down.")

    if failed:
        with pytest.raises(NetError):
            asyncio.run(go())
    else:
        asyncio.run(go())
    done = spans.named("POST ntfy.sh")
    assert (done.attributes or {})["http.response.status_code"] == status
    assert (done.status.status_code is StatusCode.ERROR) is failed
    assert TOPIC not in everything(spans)
    assert "Eva is down" not in everything(spans)


def test_a_model_call_is_one_span_without_the_question(spans: Recorded) -> None:
    answer = {"choices": [{"message": {"content": "fine"}}]}
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, content=json.dumps(answer).encode()))

    async def go() -> str:
        async with ModelClient("http://127.0.0.1:8766", transport=transport) as model:
            return await model.chat(
                [{"role": "user", "content": "private question"}], max_tokens=5, temperature=0
            )

    assert asyncio.run(go()) == "fine"
    done = spans.named("POST 127.0.0.1")
    assert dict(done.attributes or {}) == {"server.address": "127.0.0.1"}
    assert "private question" not in everything(spans)


def test_a_failed_model_call_is_a_failed_span(spans: Recorded) -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, json={"choices": []}))

    async def go() -> str:
        async with ModelClient("http://127.0.0.1:8766", transport=transport) as model:
            return await model.chat([{"role": "user", "content": "q"}], max_tokens=5, temperature=0)

    with pytest.raises(NetError):
        asyncio.run(go())
    assert (spans.named("POST 127.0.0.1").attributes or {})["error.type"] == "net.bad_response"

"""Heartbeats: one GET to the briefing's Healthchecks check, and the address stays secret."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from pydantic import SecretStr

from tests.tracing import recorded
from yaqzan.core.net import HeartbeatClient, NetError, NetErrorCode, NetPolicy, check_ping_url

UUID = "0f3c6a1e-5b7d-4c2a-9e8f-1a2b3c4d5e6f"  # made up
PING = SecretStr(f"https://hc-ping.com/{UUID}")
POLICY = NetPolicy(allowed_hosts=frozenset({"hc-ping.com"}))


def beat(handler: object, url: SecretStr = PING) -> HeartbeatClient:
    return HeartbeatClient(url, transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def test_a_ping_is_one_get_to_the_check_and_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="OK")

    with recorded() as spans:
        asyncio.run(beat(serve).ping("briefing"))
    (request,) = seen
    assert (request.method, str(request.url), request.content) == ("GET", PING.get_secret_value(), b"")
    assert request.headers["User-Agent"] == "Yaqzan-heartbeat/1"
    (span,) = spans.spans
    assert span.name == "GET hc-ping.com"
    assert UUID not in str(dict(span.attributes or {}))
    out = capsys.readouterr().err
    assert "Told Healthchecks the briefing job ran." in out
    assert UUID not in out


@pytest.mark.parametrize(
    "url",
    [
        f"http://hc-ping.com/{UUID}",  # plain http
        f"https://evil.example/{UUID}",  # not the allowlisted host
        f"https://hc-ping.com.evil.example/{UUID}",
        f"https://1.2.3.4/{UUID}",
        f"https://user:pw@hc-ping.com/{UUID}",
        f"https://hc-ping.com:8443/{UUID}",
        f"https://hc-ping.com/{UUID}?rid=1",
        f"https://hc-ping.com/{UUID}#x",
        f"https://hc-ping.com/{UUID}/fail",  # a different signal than "ran"
        f"https://hc-ping.com/{UUID}/../x",
        "https://hc-ping.com/",
        "https://hc-ping.com/ping-key/briefing",  # the slug form is not used
        "",
    ],
)
def test_odd_addresses_are_refused_without_quoting_them(url: str) -> None:
    with pytest.raises(NetError) as caught:
        check_ping_url(SecretStr(url), POLICY)
    assert caught.value.net_code is NetErrorCode.BLOCKED
    assert UUID not in str(caught.value)


def test_the_address_is_tidied() -> None:
    upper = SecretStr(f"  https://HC-PING.com/{UUID.upper()}  ")
    assert check_ping_url(upper, POLICY) == PING.get_secret_value()


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (httpx.Response(404), NetErrorCode.CLIENT_ERROR),  # the check was deleted
        (httpx.Response(302, headers={"Location": "https://elsewhere.example"}), NetErrorCode.CLIENT_ERROR),
        (httpx.ConnectError("down"), NetErrorCode.UNAVAILABLE),
        (httpx.ReadTimeout("slow"), NetErrorCode.TIMEOUT),
    ],
)
def test_a_failed_ping_says_why_once_and_never_names_the_address(
    answer: object, code: NetErrorCode, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = 0

    def serve(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[return-value]

    with pytest.raises(NetError) as caught:
        asyncio.run(beat(serve).ping("briefing"))
    assert (caught.value.net_code, calls) == (code, 1)  # never retried, never redirected
    assert UUID not in str(caught.value)
    out = capsys.readouterr().err
    assert "Could not tell Healthchecks the briefing job ran" in out
    assert UUID not in out

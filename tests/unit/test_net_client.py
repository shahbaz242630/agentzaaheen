"""The one outbound client: allowlist, https only, no redirects, size cap, retries (SECURITY NET-1).

No real network: every request goes to an ``httpx.MockTransport`` handler.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from pydantic import SecretStr

from yaqzan.core.net import JsonClient, NetError, NetErrorCode, NetPolicy, check_base_url, check_path
from yaqzan.core.sdk import SourceError, SourceErrorCode

HOST = "api.example.com"
POLICY = NetPolicy(allowed_hosts=frozenset({HOST}), max_attempts=3, max_response_bytes=1000)
FAKE_KEY = "pdl_" + "k" * 24  # built here, never a real key

Handler = Callable[[httpx.Request], httpx.Response]


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def client(handler: Handler, sleeps: Sleeps | None = None, policy: NetPolicy = POLICY) -> JsonClient:
    return JsonClient(
        f"https://{HOST}",
        policy=policy,
        headers={"Paddle-Version": "1"},
        secret_headers={"Authorization": SecretStr(f"Bearer {FAKE_KEY}")},
        transport=httpx.MockTransport(handler),
        sleep=sleeps or Sleeps(),
        jitter=lambda: 1.0,
    )


def get(
    handler: Handler, path: str = "/things", params: dict[str, str] | None = None, **kw: object
) -> object:
    async def body() -> object:
        async with client(handler, **kw) as c:  # type: ignore[arg-type]  # test helper passes known kwargs
            return await c.get_json(path, params or {})

    return asyncio.run(body())


def ok(payload: object) -> Handler:
    return lambda request: httpx.Response(200, json=payload)


# ---------------------------------------------------------------- the happy path


def test_returns_the_json_object_and_sends_headers_and_params() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [1]})

    assert get(handler, params={"after": "x_1", "updated_at[GTE]": "2026-10-01T00:00:00Z"}) == {"data": [1]}
    request = seen[0]
    assert request.method == "GET"
    assert request.url.host == HOST
    assert request.url.path == "/things"
    assert request.url.params["updated_at[GTE]"] == "2026-10-01T00:00:00Z"
    assert request.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert request.headers["Paddle-Version"] == "1"
    assert request.headers["Accept"] == "application/json"


def test_base_path_is_kept_and_trailing_slash_dropped() -> None:
    assert check_base_url(f"https://{HOST}/v2/", POLICY) == f"https://{HOST}/v2"
    assert check_base_url(f"https://{HOST.upper()}:443", POLICY) == f"https://{HOST}"


# ---------------------------------------------------------------- refused before anything is sent


@pytest.mark.parametrize(
    "url",
    [
        f"http://{HOST}",
        "https://evil.example.org",
        "https://169.254.169.254",
        "https://[::1]",
        f"https://user:pw@{HOST}",
        f"https://{HOST}?x=1",
        f"https://{HOST}#frag",
        f"https://{HOST}:8443",
        "https://",
    ],
)
def test_bad_base_urls_are_refused(url: str) -> None:
    with pytest.raises(NetError) as caught:
        check_base_url(url, POLICY)
    assert caught.value.net_code is NetErrorCode.BLOCKED
    assert caught.value.code is SourceErrorCode.REFUSED


@pytest.mark.parametrize(
    "path",
    [
        "things",
        "//evil.example.org/x",
        "/a/../b",
        "/a?b=1",
        "/a#b",
        "/a\\b",
        "/a b",
        "/@evil",
        "/x://y",
        "/" + "a" * 600,
    ],
)
def test_odd_paths_are_refused_without_a_request(path: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent for a refused path")

    with pytest.raises(NetError) as caught:
        get(handler, path=path)
    assert caught.value.net_code is NetErrorCode.BLOCKED
    with pytest.raises(NetError):
        check_path(path)


# ---------------------------------------------------------------- refused responses


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_are_never_followed(status: int) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(status, headers={"Location": "https://169.254.169.254/latest"})

    with pytest.raises(NetError) as caught:
        get(handler)
    assert caught.value.net_code is NetErrorCode.BLOCKED
    assert caught.value.status == status
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("status", "code", "sdk_code"),
    [
        (401, NetErrorCode.AUTH, SourceErrorCode.AUTH),
        (403, NetErrorCode.AUTH, SourceErrorCode.AUTH),
        (400, NetErrorCode.CLIENT_ERROR, SourceErrorCode.REFUSED),
        (404, NetErrorCode.CLIENT_ERROR, SourceErrorCode.REFUSED),
        (204, NetErrorCode.CLIENT_ERROR, SourceErrorCode.REFUSED),
    ],
)
def test_refusals_are_not_retried(status: int, code: NetErrorCode, sdk_code: SourceErrorCode) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, json={"error": {"detail": "secret-ish detail"}})

    with pytest.raises(NetError) as caught:
        get(handler)
    assert (caught.value.net_code, caught.value.code, caught.value.status) == (code, sdk_code, status)
    assert isinstance(caught.value, SourceError)
    assert "secret-ish" not in str(caught.value)
    assert len(calls) == 1


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b"\xff\xfe", b"42"])
def test_non_object_bodies_are_bad_responses(body: bytes) -> None:
    with pytest.raises(NetError) as caught:
        get(lambda request: httpx.Response(200, content=body))
    assert caught.value.net_code is NetErrorCode.BAD_RESPONSE


def test_declared_oversize_is_refused() -> None:
    big = json.dumps({"a": "x" * 2000}).encode()
    with pytest.raises(NetError) as caught:
        get(lambda request: httpx.Response(200, content=big, headers={"Content-Length": str(len(big))}))
    assert caught.value.net_code is NetErrorCode.TOO_LARGE


def test_streamed_oversize_is_refused_even_without_a_length() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(20):
            yield b"x" * 100

    with pytest.raises(NetError) as caught:
        get(lambda request: httpx.Response(200, content=chunks()))
    assert caught.value.net_code is NetErrorCode.TOO_LARGE


# ---------------------------------------------------------------- retries


def test_retries_server_errors_then_succeeds_with_growing_delays() -> None:
    statuses = iter([503, 500])
    sleeps = Sleeps()

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses, 200)
        return httpx.Response(status, json={"ok": True})

    assert get(handler, sleeps=sleeps) == {"ok": True}
    assert sleeps.delays == [0.5, 1.0]


def test_retry_after_is_honoured_and_capped() -> None:
    sleeps = Sleeps()
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(429, headers={"Retry-After": "9999"}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses, httpx.Response(200, json={}))

    assert get(handler, sleeps=sleeps) == {}
    assert sleeps.delays == [7.0, POLICY.max_delay_s]


def test_gives_up_after_max_attempts_with_rate_limited() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "soon"})

    with pytest.raises(NetError) as caught:
        get(handler)
    assert caught.value.net_code is NetErrorCode.RATE_LIMITED
    assert caught.value.code is SourceErrorCode.RATE_LIMITED
    assert len(calls) == POLICY.max_attempts


def test_gives_up_on_persistent_server_errors() -> None:
    with pytest.raises(NetError) as caught:
        get(lambda request: httpx.Response(502))
    assert caught.value.net_code is NetErrorCode.SERVER_ERROR
    assert caught.value.code is SourceErrorCode.UNAVAILABLE


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (httpx.ReadTimeout("slow"), NetErrorCode.TIMEOUT),
        (httpx.ConnectError("down"), NetErrorCode.UNAVAILABLE),
    ],
)
def test_transport_failures_are_retried_then_reported(exc: Exception, code: NetErrorCode) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise exc

    with pytest.raises(NetError) as caught:
        get(handler)
    assert caught.value.net_code is code
    assert len(calls) == POLICY.max_attempts


def test_a_transport_failure_then_success_recovers() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("blip")
        return httpx.Response(200, json={"ok": 1})

    assert get(handler) == {"ok": 1}


# ---------------------------------------------------------------- the key never leaks


def test_key_never_appears_in_errors_or_retry_logs(capsys: pytest.CaptureFixture[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"X-Echo": request.headers["Authorization"]})

    with pytest.raises(NetError) as caught:
        get(handler)
    assert FAKE_KEY not in str(caught.value)
    assert FAKE_KEY not in repr(caught.value.args)
    logged = capsys.readouterr().err
    assert "net_retry" in logged
    assert FAKE_KEY not in logged


@pytest.mark.parametrize("ip", ["169.254.169.254", "10.0.0.1", "[::1]"])
def test_an_ip_literal_is_refused_even_if_someone_allowlists_it(ip: str) -> None:
    careless = NetPolicy(allowed_hosts=frozenset({ip, ip.strip("[]")}))
    with pytest.raises(NetError) as caught:
        check_base_url(f"https://{ip}", careless)
    assert caught.value.net_code is NetErrorCode.BLOCKED
    assert "ip-literal" in str(caught.value)

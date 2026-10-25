"""The model client: loopback http or allowlisted https, one POST, no retries (ADR-024).

No real network and no real model: every request goes to an ``httpx.MockTransport`` handler.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from agentzaaheen.core.net import ModelClient, NetError, NetErrorCode, check_model_url

LOCAL = "http://127.0.0.1:8766"
SERVER = "models.example.com"
FAKE_KEY = "mdl_" + "k" * 24  # built here, never a real key
QUESTION = [{"role": "user", "content": "How is revenue?"}]

Handler = Callable[[httpx.Request], httpx.Response]


def answer(text: str) -> Handler:
    return lambda request: httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": text}}]}
    )


def ask(handler: Handler, base: str = LOCAL, **kw: object) -> str:
    async def body() -> str:
        async with ModelClient(base, transport=httpx.MockTransport(handler), **kw) as c:  # type: ignore[arg-type]  # test helper passes known kwargs
            return await c.chat(QUESTION, max_tokens=64, temperature=0.2)

    return asyncio.run(body())


def failure(handler: Handler, **kw: object) -> NetError:
    with pytest.raises(NetError) as caught:
        ask(handler, **kw)  # type: ignore[arg-type]  # test helper passes known kwargs
    return caught.value


# ---------------------------------------------------------------- the happy path


def test_posts_one_chat_request_and_returns_the_text() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer("Revenue is steady.")(request)

    assert ask(handler) == "Revenue is steady."
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "http://127.0.0.1:8766/v1/chat/completions"
    assert "authorization" not in request.headers
    assert json.loads(request.content) == {
        "model": "local",
        "messages": QUESTION,
        "max_tokens": 64,
        "temperature": 0.2,
        "stream": False,
    }


def test_only_role_and_content_are_sent() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return answer("ok")(request)

    async def body() -> None:
        async with ModelClient(LOCAL, transport=httpx.MockTransport(handler)) as c:
            await c.chat([{"role": "user", "content": "hi", "name": "x"}], max_tokens=8, temperature=0.0)

    asyncio.run(body())
    assert seen[0]["messages"] == [{"role": "user", "content": "hi"}]


def test_an_allowlisted_https_server_gets_the_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer("ok")(request)

    ask(handler, base=f"https://{SERVER}", allowed_hosts=frozenset({SERVER}), api_key=SecretStr(FAKE_KEY))
    assert seen[0].url.host == SERVER
    assert seen[0].url.scheme == "https"
    assert seen[0].headers["Authorization"] == f"Bearer {FAKE_KEY}"


# ---------------------------------------------------------------- where it may go


@pytest.mark.parametrize(
    ("url", "cleaned"),
    [
        ("http://127.0.0.1:8766", "http://127.0.0.1:8766"),
        ("http://127.0.0.1:8766/", "http://127.0.0.1:8766"),
        (f"https://{SERVER}", f"https://{SERVER}"),
        (f"https://{SERVER.upper()}:443/", f"https://{SERVER}"),
    ],
)
def test_accepted_urls(url: str, cleaned: str) -> None:
    assert check_model_url(url, frozenset({SERVER})) == cleaned


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",  # loopback needs an explicit port
        "http://localhost:8766",  # a name could be re-pointed; only the literal loopback address
        "http://10.0.0.5:8766",  # plain http off the machine
        f"http://{SERVER}:8766",
        "https://127.0.0.1:8766",  # https must be a named, allowlisted host
        "https://other.example.com",  # not on the allowlist
        f"https://{SERVER}:8443",  # odd port
        f"https://user:pw@{SERVER}",  # credentials in the url
        f"https://{SERVER}/v1",  # a path
        f"https://{SERVER}?x=1",  # a query
        f"ftp://{SERVER}",
    ],
)
def test_refused_urls(url: str) -> None:
    with pytest.raises(NetError) as caught:
        check_model_url(url, frozenset({SERVER}))
    assert caught.value.net_code is NetErrorCode.BLOCKED


def test_a_key_is_never_sent_over_plain_http() -> None:
    with pytest.raises(NetError) as caught:
        ModelClient(LOCAL, api_key=SecretStr(FAKE_KEY))
    assert caught.value.net_code is NetErrorCode.BLOCKED
    assert FAKE_KEY not in str(caught.value)


# ---------------------------------------------------------------- failures


def test_a_redirect_is_refused_not_followed() -> None:
    error = failure(lambda r: httpx.Response(302, headers={"Location": "https://evil.example/"}))
    assert error.net_code is NetErrorCode.BLOCKED
    assert error.status == 302


def test_a_server_error_is_not_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    error = failure(handler)
    assert calls == [1]
    assert error.net_code is NetErrorCode.CLIENT_ERROR
    assert error.status == 503
    assert error.operation == "POST /v1/chat/completions"
    assert (error.attempt, error.attempts) == (1, 1)


def test_a_refused_key_is_an_auth_error() -> None:
    error = failure(
        lambda r: httpx.Response(401),
        base=f"https://{SERVER}",
        allowed_hosts=frozenset({SERVER}),
        api_key=SecretStr(FAKE_KEY),
    )
    assert error.net_code is NetErrorCode.AUTH
    assert FAKE_KEY not in str(error)


def test_a_timeout_says_so() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    assert failure(handler).net_code is NetErrorCode.TIMEOUT


def test_a_server_that_is_not_running_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert failure(handler).net_code is NetErrorCode.UNAVAILABLE


def test_an_oversized_answer_is_refused() -> None:
    error = failure(answer("x" * 2000), max_response_bytes=1000)
    assert error.net_code is NetErrorCode.TOO_LARGE


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": ["text"]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"text": "old completion shape"}]},
    ],
)
def test_an_answer_without_message_text_is_a_bad_response(payload: object) -> None:
    error = failure(lambda r: httpx.Response(200, json=payload))
    assert error.net_code is NetErrorCode.BAD_RESPONSE


def test_a_failure_never_carries_the_prompt_or_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json: How is revenue?")

    error = failure(handler)
    assert "How is revenue" not in str(error)
    assert "How is revenue" not in repr(error.args)

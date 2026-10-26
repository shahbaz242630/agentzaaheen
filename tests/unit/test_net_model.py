"""The model client: loopback http or allowlisted https, one POST, no retries (ADR-024).

No real network and no real model: every request goes to an ``httpx.MockTransport`` handler.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import httpx
import pytest
from pydantic import SecretStr

from agentzaaheen.core.net import ModelClient, NetError, NetErrorCode, check_model_url
from agentzaaheen.core.sdk import SourceError, SourceErrorCode

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
        "stream": True,  # so Cloudflare sees the answer start (and pings) within 100 s
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


# ---------------------------------------------------------------- streamed answers


def sse(*events: object, done: bool = True) -> bytes:
    """A stream as llama.cpp sends it: a ping while it reads the prompt, then one ``data:`` line per piece."""
    lines = [":", ""]  # the 30-second ping, then the blank line that ends it
    for event in events:
        lines += [f"data: {json.dumps(event)}", ""]
    if done:
        lines += ["data: [DONE]", ""]
    return ("\n".join(lines) + "\n").encode()


def piece(text: str) -> dict[str, object]:
    return {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}


ROLE = {"choices": [{"index": 0, "delta": {"role": "assistant", "content": None}}]}
FINISH = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
PROGRESS = {"choices": [], "prompt_progress": {"total": 900, "cache": 0, "processed": 512, "time_ms": 15000}}


class Chunks(httpx.AsyncByteStream):
    """``body`` arriving ``size`` bytes at a time, as a network may cut it."""

    def __init__(self, body: bytes, size: int) -> None:
        self._body, self._size = body, size

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for i in range(0, len(self._body), self._size):
            yield self._body[i : i + self._size]


def event_stream(stream: httpx.AsyncByteStream) -> httpx.Response:
    return httpx.Response(200, headers={"Content-Type": "text/event-stream; charset=utf-8"}, stream=stream)


def streamed(body: bytes, chunk: int | None = None) -> Handler:
    """Answer with ``body`` as an event stream, in ``chunk``-byte pieces (default: all at once)."""
    return lambda request: event_stream(Chunks(body, chunk or len(body)))


def test_a_streamed_answer_is_joined_into_one_text() -> None:
    body = sse(ROLE, PROGRESS, piece("Revenue "), piece("is "), piece("steady."), FINISH)
    assert ask(streamed(body)) == "Revenue is steady."


@pytest.mark.parametrize("chunk", [1, 3, 7, 64])
def test_pieces_split_anywhere_by_the_network_still_join_correctly(chunk: int) -> None:
    body = sse(ROLE, piece("Ventes "), piece("été — "), piece("مرحبا"), FINISH)  # multi-byte letters too
    assert ask(streamed(body, chunk)) == "Ventes été — مرحبا"


def test_windows_line_ends_are_read_the_same() -> None:
    body = sse(piece("ok"), FINISH).replace(b"\n", b"\r\n")
    assert ask(streamed(body)) == "ok"


def test_reasoning_pieces_are_not_part_of_the_answer() -> None:
    thinking = {"choices": [{"index": 0, "delta": {"reasoning_content": "secret plan"}}]}
    assert ask(streamed(sse(thinking, piece("Answer."), FINISH))) == "Answer."


def test_a_stream_cut_off_before_done_is_refused_not_half_used() -> None:
    error = failure(streamed(sse(piece("Revenue is "), done=False)))
    assert error.net_code is NetErrorCode.BAD_RESPONSE
    assert "Revenue" not in str(error)


def test_an_error_inside_the_stream_is_a_server_error() -> None:
    oops = {"error": {"code": 500, "message": "context full: How is revenue?", "type": "server_error"}}
    error = failure(streamed(sse(piece("Rev"), oops)))
    assert error.net_code is NetErrorCode.SERVER_ERROR
    assert "How is revenue" not in str(error)


BROKEN_UTF8 = b"\xc3\x28"


@pytest.mark.parametrize("line", [b"data: not json", b"data: [1, 2]", b"data: " + BROKEN_UTF8])
def test_a_garbled_piece_is_a_bad_response(line: bytes) -> None:
    body = sse(piece("ok")).replace(b"data: [DONE]", line + b"\n\ndata: [DONE]")
    assert failure(streamed(body)).net_code is NetErrorCode.BAD_RESPONSE


def test_an_oversized_stream_is_refused_as_it_arrives() -> None:
    body = sse(*[piece("x" * 100) for _ in range(30)], FINISH)
    error = failure(streamed(body, chunk=50), max_response_bytes=1000)
    assert error.net_code is NetErrorCode.TOO_LARGE


def test_the_whole_call_has_one_deadline_even_while_pings_keep_arriving() -> None:
    class Pings(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            while True:  # a server that pings for ever and never answers
                yield b":\n\n"
                await asyncio.sleep(0.01)

    assert failure(lambda request: event_stream(Pings()), timeout_s=0.2).net_code is NetErrorCode.TIMEOUT


# ---------------------------------------------------------------- our server, behind Cloudflare Access

TOKEN_ID = "0" * 32 + ".access"  # built here, never a real token
TOKEN_SECRET = "f" * 64


@dataclass(frozen=True)
class Token:
    client_id: str
    client_secret: SecretStr


def tokens(*secrets: str) -> tuple[Callable[[], Awaitable[Token]], list[int]]:
    """A token source that hands out ``secrets`` in turn and counts how often it was opened."""
    opened: list[int] = []

    async def source() -> Token:
        opened.append(1)
        return Token(TOKEN_ID, SecretStr(secrets[min(len(opened), len(secrets)) - 1]))

    return source, opened


def test_the_server_token_is_opened_for_each_call_and_sent_as_cloudflares_two_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer("ok")(request)

    source, opened = tokens(TOKEN_SECRET, "e" * 64)

    async def body() -> None:
        async with ModelClient(
            f"https://{SERVER}",
            allowed_hosts=frozenset({SERVER}),
            access=source,
            transport=httpx.MockTransport(handler),
        ) as c:
            for _ in range(2):
                await c.chat(QUESTION, max_tokens=64, temperature=0.2)

    asyncio.run(body())
    assert len(opened) == 2  # once per call: a replaced token is used at once
    assert [r.headers["CF-Access-Client-Id"] for r in seen] == [TOKEN_ID, TOKEN_ID]
    assert [r.headers["CF-Access-Client-Secret"] for r in seen] == [TOKEN_SECRET, "e" * 64]
    assert "Authorization" not in seen[0].headers


def test_without_a_token_source_no_access_headers_are_sent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer("ok")(request)

    ask(handler)
    assert "CF-Access-Client-Id" not in seen[0].headers
    assert "CF-Access-Client-Secret" not in seen[0].headers


def test_a_token_that_cannot_be_opened_sends_nothing_and_is_an_auth_error() -> None:
    seen: list[httpx.Request] = []

    async def locked() -> Token:
        raise SourceError(SourceErrorCode.AUTH, "no token saved", detail_code="credential.missing")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer("ok")(request)

    caught = failure(handler, base=f"https://{SERVER}", allowed_hosts=frozenset({SERVER}), access=locked)
    assert caught.net_code is NetErrorCode.AUTH
    assert seen == []


def test_the_server_token_is_never_sent_over_plain_http() -> None:
    source, opened = tokens(TOKEN_SECRET)
    with pytest.raises(NetError) as caught:
        ModelClient(LOCAL, access=source)
    assert caught.value.net_code is NetErrorCode.BLOCKED
    assert opened == []


def test_cloudflares_login_redirect_is_refused_not_followed() -> None:
    def login(_: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://spring-river-c250.cloudflareaccess.com/"})

    source, _ = tokens(TOKEN_SECRET)
    caught = failure(login, base=f"https://{SERVER}", allowed_hosts=frozenset({SERVER}), access=source)
    assert caught.net_code is NetErrorCode.BLOCKED
    assert caught.status == 302
    assert TOKEN_SECRET not in str(caught)

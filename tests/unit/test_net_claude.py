"""The one door to Claude: one Messages POST to api.anthropic.com, nothing else (ADR-033, F10 slice 2a).

No real network and no real key: every request goes to an ``httpx.MockTransport`` handler.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from agentzaaheen.core.net import CLAUDE_HOST, ClaudeClient, ClaudeReply, NetError, NetErrorCode
from agentzaaheen.core.net.claude import API_VERSION

FAKE_KEY = "test-claude-" + "k" * 24  # built here, never a real key
PINNED = frozenset({"claude-opus-5-5", "claude-sonnet-5-5"})
TASK = [{"role": "user", "content": "Review this week's findings."}]

Handler = Callable[[httpx.Request], httpx.Response]


def reply_body(**usage: object) -> dict[str, object]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": "Two findings matter."},
            {"type": "text", "text": " The rest can wait."},
        ],
        "stop_reason": "end_turn",
        "usage": usage or {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 8000},
    }


def ok(**usage: object) -> Handler:
    return lambda request: httpx.Response(200, json=reply_body(**usage))


class Keys:
    def __init__(self) -> None:
        self.opened = 0

    async def __call__(self) -> SecretStr:
        self.opened += 1
        return SecretStr(FAKE_KEY)


def send(
    handler: Handler, *, model: str = "claude-opus-5-5", keys: Keys | None = None, **kw: object
) -> ClaudeReply:
    async def body() -> ClaudeReply:
        async with ClaudeClient(keys or Keys(), models=PINNED, transport=httpx.MockTransport(handler)) as c:
            return await c.message(
                model=model,
                system=["You are the Head of Engineering.", "Rules: read only."],
                messages=TASK,
                max_tokens=2000,
                **kw,  # type: ignore[arg-type]  # test helper passes known kwargs
            )

    return asyncio.run(body())


def failure(handler: Handler, **kw: object) -> NetError:
    with pytest.raises(NetError) as caught:
        send(handler, **kw)  # type: ignore[arg-type]  # test helper passes known kwargs
    return caught.value


def test_posts_one_messages_request_with_the_key_and_version_and_reads_the_text() -> None:
    seen: list[httpx.Request] = []
    keys = Keys()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok()(request)

    reply = send(handler, keys=keys, effort="medium")
    assert reply.text == "Two findings matter. The rest can wait."
    assert (reply.input_tokens, reply.output_tokens, reply.cache_read_tokens) == (1200, 300, 8000)
    assert reply.stop_reason == "end_turn"
    [request] = seen
    assert (request.method, str(request.url)) == ("POST", f"https://{CLAUDE_HOST}/v1/messages")
    assert request.headers["x-api-key"] == FAKE_KEY
    assert request.headers["anthropic-version"] == API_VERSION == "2026-10-21"
    assert keys.opened == 1
    body = json.loads(request.content)
    assert body["model"] == "claude-opus-5-5"
    assert body["max_tokens"] == 2000
    assert body["messages"] == TASK
    assert body["output_config"] == {"effort": "medium"}
    assert [block["text"] for block in body["system"]] == [
        "You are the Head of Engineering.",
        "Rules: read only.",
    ]
    assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}  # the fixed prefix can be cached
    assert "cache_control" not in body["system"][0]
    for refused in ("temperature", "top_p", "top_k", "thinking", "fallbacks"):
        assert refused not in body


def test_the_key_is_asked_for_on_every_call() -> None:
    keys = Keys()

    async def body() -> None:
        async with ClaudeClient(keys, models=PINNED, transport=httpx.MockTransport(ok())) as c:
            for _ in range(2):
                await c.message(model="claude-opus-5-5", system=[], messages=TASK, max_tokens=10)

    asyncio.run(body())
    assert keys.opened == 2


def test_no_system_prefix_sends_no_system_field() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return ok()(request)

    async def body() -> None:
        async with ClaudeClient(Keys(), models=PINNED, transport=httpx.MockTransport(handler)) as c:
            await c.message(model="claude-sonnet-5-5", system=["", ""], messages=TASK, max_tokens=10)

    asyncio.run(body())
    assert "system" not in seen[0]
    assert "output_config" not in seen[0]


def test_cache_writes_are_split_by_how_long_they_are_kept() -> None:
    reply = send(
        ok(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=900,
            cache_creation={"ephemeral_5m_input_tokens": 600, "ephemeral_1h_input_tokens": 300},
        )
    )
    assert (reply.cache_write_5m_tokens, reply.cache_write_1h_tokens) == (600, 300)


def test_cache_writes_with_no_split_are_priced_at_the_dearer_hour_rate() -> None:
    reply = send(ok(input_tokens=10, output_tokens=5, cache_creation_input_tokens=900))
    assert (reply.cache_write_5m_tokens, reply.cache_write_1h_tokens) == (0, 900)


def test_a_model_that_is_not_pinned_is_refused_before_anything_is_sent() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return ok()(request)

    error = failure(handler, model="claude-fable-5-1")
    assert error.net_code is NetErrorCode.BLOCKED
    assert calls == []


def test_an_unknown_effort_is_refused() -> None:
    assert failure(ok(), effort="extreme").net_code is NetErrorCode.BLOCKED


def test_a_client_with_no_pinned_models_cannot_be_made() -> None:
    with pytest.raises(NetError):
        ClaudeClient(Keys(), models=frozenset())


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, NetErrorCode.AUTH),
        (403, NetErrorCode.AUTH),
        (429, NetErrorCode.RATE_LIMITED),
        (529, NetErrorCode.RATE_LIMITED),
        (500, NetErrorCode.SERVER_ERROR),
        (400, NetErrorCode.CLIENT_ERROR),
        (402, NetErrorCode.CLIENT_ERROR),
        (307, NetErrorCode.BLOCKED),
    ],
)
def test_each_failure_has_a_code_and_status_and_never_the_key_or_text(
    status: int, code: NetErrorCode
) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            status, json={"type": "error", "error": {"message": "Review this week's findings."}}
        )

    error = failure(handler)
    assert (error.net_code, error.status) == (code, status)
    assert calls == [1]  # never retried
    assert FAKE_KEY not in str(error)
    assert "findings" not in str(error)


def test_an_answer_over_the_size_cap_is_refused() -> None:
    async def body() -> None:
        handler = ok()
        async with ClaudeClient(
            Keys(), models=PINNED, max_response_bytes=50, transport=httpx.MockTransport(handler)
        ) as c:
            await c.message(model="claude-opus-5-5", system=[], messages=TASK, max_tokens=10)

    with pytest.raises(NetError) as caught:
        asyncio.run(body())
    assert caught.value.net_code is NetErrorCode.TOO_LARGE


@pytest.mark.parametrize(
    "payload",
    [
        {
            "content": "not a list",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "stop_reason": "end_turn",
        },
        {"content": [], "usage": {"input_tokens": 1}, "stop_reason": "end_turn"},
        {"content": [], "usage": {"input_tokens": -1, "output_tokens": 1}, "stop_reason": "end_turn"},
        {"content": [], "usage": {"input_tokens": True, "output_tokens": 1}, "stop_reason": "end_turn"},
        {"content": [], "usage": {"input_tokens": 1, "output_tokens": 1}},
    ],
)
def test_an_answer_without_usable_counts_is_a_bad_response(payload: dict[str, object]) -> None:
    error = failure(lambda request: httpx.Response(200, json=payload))
    assert error.net_code is NetErrorCode.BAD_RESPONSE


def test_a_timeout_and_a_dead_line_have_their_own_codes() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    assert failure(timeout).net_code is NetErrorCode.TIMEOUT
    assert failure(dead).net_code is NetErrorCode.UNAVAILABLE


def test_a_redirect_is_never_followed_even_to_a_real_address() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == CLAUDE_HOST:
            return httpx.Response(307, headers={"Location": "https://collector.example.com/v1/messages"})
        return ok()(request)

    assert failure(handler).net_code is NetErrorCode.BLOCKED
    assert seen == [f"https://{CLAUDE_HOST}/v1/messages"]


def test_only_text_blocks_become_the_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = reply_body()
        body["content"] = [
            {"type": "server_tool_result", "text": "not part of the answer"},
            {"type": "text", "text": "The answer."},
        ]
        return httpx.Response(200, json=body)

    assert send(handler).text == "The answer."

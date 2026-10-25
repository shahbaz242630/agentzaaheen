"""The one door to Claude: a Messages API request to api.anthropic.com, nothing else (ADR-033).

Every Claude call in Agent Zaaheen goes through :class:`ClaudeClient`, and only after
the gateway's ``ModelGate`` has cleared it (level, customer data, spend caps).
The client:

- talks only to ``https://api.anthropic.com`` (fixed: no other host, no plain
  http, no redirects, no proxy settings from the environment);
- sends only the pinned model ids it was given;
- asks for the key once per call (the broker opens it; this module never
  can) and keeps no copy;
- caps the answer's size and returns the text plus the API's own token
  counts, which is what the spend caps are worked out from;
- is never retried: a retry costs credit, and a run that could not reach
  Claude is marked "did not run" instead;
- fails with a code and the HTTP status only, never the prompt, the answer
  or the key.

The API version is ``2026-10-21``, which refuses sampling settings such as
``temperature``, so none are sent. Opus 5.5 always thinks; its thinking
blocks come back empty and are ignored, and their tokens are counted in
``output_tokens`` (billed as output).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Self

import httpx
from pydantic import SecretStr

from agentzaaheen.core.net.client import decode_object, read_capped
from agentzaaheen.core.net.errors import NetError, NetErrorCode
from agentzaaheen.core.sdk import elapsed_ms, get_logger, say, span

CLAUDE_HOST = "api.anthropic.com"
MESSAGES_PATH = "/v1/messages"
API_VERSION = "2026-10-21"
_OPERATION = f"POST {MESSAGES_PATH}"
_AUTH = frozenset({401, 403})
_BUSY = frozenset({429, 529})
_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})

KeySource = Callable[[], Awaitable[SecretStr]]


@dataclass(frozen=True)
class ClaudeReply:
    """Claude's answer text and what the call used, as the API counted it."""

    text: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_5m_tokens: int
    cache_write_1h_tokens: int


class ClaudeClient:
    """Sends one Messages request at a time to Claude, for the pinned models only."""

    def __init__(  # each is a separate, explicit knob
        self,
        key: KeySource,
        *,
        models: frozenset[str],
        timeout_s: float = 300.0,
        max_response_bytes: int = 4 * 1024 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not models:
            raise NetError(NetErrorCode.BLOCKED, "the Claude client needs its pinned model ids")
        self._key = key
        self._models = models
        self._limit = max_response_bytes
        self._http = httpx.AsyncClient(
            base_url=f"https://{CLAUDE_HOST}",
            headers={"Accept": "application/json", "anthropic-version": API_VERSION},
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._log = get_logger("agentzaaheen.net.claude", host=CLAUDE_HOST)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the connection pool."""
        await self._http.aclose()

    async def message(  # the request's own parts
        self,
        *,
        model: str,
        system: Sequence[str],
        messages: Sequence[Mapping[str, str]],
        max_tokens: int,
        effort: str | None = None,
    ) -> ClaudeReply:
        """Ask ``model`` once. ``system`` is the fixed prefix (role pack, rules), sent so it can be cached."""
        if model not in self._models:
            raise NetError(NetErrorCode.BLOCKED, "that model id is not pinned for this client")
        if effort is not None and effort not in _EFFORTS:
            raise NetError(NetErrorCode.BLOCKED, "that effort level does not exist")
        body = _body(model, system, messages, max_tokens, effort)
        started = time.monotonic()
        try:
            with span(f"POST {CLAUDE_HOST}", kind="client", attributes={"server.address": CLAUDE_HOST}):
                reply = await self._ask(body)
                say(self._log, "claude_answered", model=model, duration_ms=elapsed_ms(started))
                return reply
        except NetError as exc:
            say(
                self._log,
                "claude_call_failed",
                model=model,
                source="anthropic",
                code=exc.net_code.value,
                status=exc.status,
            )
            raise exc.at(_OPERATION, 1, 1) from None

    async def _ask(self, body: dict[str, object]) -> ClaudeReply:
        key = await self._key()
        headers = {"x-api-key": key.get_secret_value()}
        try:
            async with self._http.stream("POST", MESSAGES_PATH, json=body, headers=headers) as response:
                _refuse(response.status_code)
                raw = await read_capped(response, self._limit)
        except httpx.TimeoutException:
            raise NetError(NetErrorCode.TIMEOUT, "Claude took too long") from None
        except httpx.TransportError:
            raise NetError(NetErrorCode.UNAVAILABLE, "could not reach Claude") from None
        finally:
            del headers, key
        return _reply(decode_object(raw))


def _body(
    model: str,
    system: Sequence[str],
    messages: Sequence[Mapping[str, str]],
    max_tokens: int,
    effort: str | None,
) -> dict[str, object]:
    blocks: list[dict[str, object]] = [{"type": "text", "text": text} for text in system if text]
    if blocks:
        blocks[-1]["cache_control"] = {"type": "ephemeral"}  # the prefix up to here can be cached
    body: dict[str, object] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
    }
    if blocks:
        body["system"] = blocks
    if effort is not None:
        body["output_config"] = {"effort": effort}
    return body


def _refuse(status: int) -> None:
    if status == 200:  # only a plain OK carries a body we trust
        return
    if 300 <= status < 400:  # the 3xx range
        raise NetError(NetErrorCode.BLOCKED, "redirects are never followed", status=status)
    if status in _AUTH:
        raise NetError(NetErrorCode.AUTH, "Claude refused the key", status=status)
    if status in _BUSY:
        raise NetError(NetErrorCode.RATE_LIMITED, "Claude is busy or rate-limited us", status=status)
    if status >= 500:  # the server's own errors
        raise NetError(NetErrorCode.SERVER_ERROR, "Claude had an error", status=status)
    raise NetError(NetErrorCode.CLIENT_ERROR, "Claude refused the request", status=status)


def _reply(payload: dict[str, object]) -> ClaudeReply:
    content, usage, stop = payload.get("content"), payload.get("usage"), payload.get("stop_reason")
    if not isinstance(content, list) or not isinstance(usage, dict) or not isinstance(stop, str):
        raise NetError(NetErrorCode.BAD_RESPONSE, "the answer is not a Messages reply", status=200)
    text = "".join(
        str(block["text"])
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    )
    written = usage.get("cache_creation")
    by_ttl = written if isinstance(written, dict) else {}
    write_5m = _count(by_ttl, "ephemeral_5m_input_tokens")
    write_1h = (
        _count(by_ttl, "ephemeral_1h_input_tokens")
        if by_ttl
        else _count(usage, "cache_creation_input_tokens")  # no split given: priced at the dearer 1-hour rate
    )
    return ClaudeReply(
        text=text,
        stop_reason=stop[:40],
        input_tokens=_count(usage, "input_tokens", required=True),
        output_tokens=_count(usage, "output_tokens", required=True),
        cache_read_tokens=_count(usage, "cache_read_input_tokens"),
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
    )


def _count(usage: Mapping[str, object], name: str, *, required: bool = False) -> int:
    value = usage.get(name)
    if value is None and not required:
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise NetError(NetErrorCode.BAD_RESPONSE, "the answer's token counts are not usable", status=200)
    return value

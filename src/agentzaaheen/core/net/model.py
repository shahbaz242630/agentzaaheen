"""The model client: one chat request to an OpenAI-compatible model server, nothing else.

The only POST in the core (ADR-024). It may reach:

- our own machine at ``http://127.0.0.1:<port>`` (llama.cpp on the laptop or
  the server itself; loopback never leaves the machine), or
- a named host on this client's allowlist over https (our server's model,
  reached through the tunnel; later an opt-in cloud model, ADR-023).

Our server's model sits behind Cloudflare Access, which refuses every request
without the laptop's service token. With ``access``, the token is opened fresh
for each call (so a replaced token is used at once and nothing keeps a copy)
and sent as Cloudflare's two headers; it is never sent over plain http. A token
that cannot be opened ends the call as ``net.auth`` before anything is sent.
The model itself answers only calls that carry its own key: with ``model_key``,
that key is opened the same way for each call and sent as ``Authorization:
Bearer``, under the same rules.

The answer is asked for as a stream (``stream: true``). Cloudflare drops a
request whose answer has not started within about 100 seconds, and a long
prompt on the server's CPU takes minutes to read. Streamed, llama.cpp sends its
headers as soon as it starts reading the prompt and a ``:`` ping every 30
seconds until the first word, so the call stays open. The pieces are joined here
and the caller still gets one text. A server that ignores ``stream`` and sends
one JSON answer is accepted too.

Redirects are refused, the answer is size-capped, a stream must end with
``[DONE]`` (one cut off part way is refused, never half-used), the whole call
has one deadline, and a failure carries a code, never the prompt, the answer or
a key. A model call is not retried: on a CPU it can take a minute, and the
person asking can simply ask again.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import TracebackType
from typing import Protocol, Self
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from agentzaaheen.core.net.client import (
    NetPolicy,
    check_host,
    decode_object,
    read_capped,
    refuse_status,
)
from agentzaaheen.core.net.errors import NetError, NetErrorCode
from agentzaaheen.core.sdk import SourceError, elapsed_ms, get_logger, say, span

LOOPBACK = "127.0.0.1"
SERVER_MODEL_HOST = "llm.agentzaaheen.com"  # our server's model, through the Cloudflare tunnel
CHAT_PATH = "/v1/chat/completions"
_OPERATION = f"POST {CHAT_PATH}"
ACCESS_ID_HEADER = "CF-Access-Client-Id"
ACCESS_SECRET_HEADER = "CF-Access-Client-Secret"  # noqa: S105  # nosec B105  # a header name, not a secret
STREAM_TYPE = "text/event-stream"
_DATA = b"data:"
_DONE = b"[DONE]"


class AccessToken(Protocol):
    """A Cloudflare Access service token (the policy layer's ``ServiceToken`` in real use)."""

    @property
    def client_id(self) -> str:
        """The token's Client ID (``….access``)."""
        ...

    @property
    def client_secret(self) -> SecretStr:
        """The token's Client Secret."""
        ...


AccessSource = Callable[[], Awaitable[AccessToken]]
KeySource = Callable[[], Awaitable[SecretStr]]


def check_model_url(base_url: str, allowed_hosts: frozenset[str]) -> str:
    """Return the cleaned base url, or raise ``NetError(BLOCKED)``.

    Plain http is allowed only to ``127.0.0.1`` with an explicit port; anything
    else must be https to a named, allowlisted host.
    """
    parts = urlsplit(base_url)
    if parts.username is not None or parts.password is not None:
        raise NetError(NetErrorCode.BLOCKED, "credentials are never put in a url")
    if parts.query or parts.fragment or parts.path.strip("/"):
        raise NetError(NetErrorCode.BLOCKED, "a model url is just a scheme, host and port")
    if parts.scheme == "http":
        if parts.hostname != LOOPBACK or parts.port is None:
            raise NetError(NetErrorCode.BLOCKED, "plain http is only for 127.0.0.1 with a port")
        return f"http://{LOOPBACK}:{parts.port}"
    if parts.scheme != "https":
        raise NetError(NetErrorCode.BLOCKED, "only https (or http to 127.0.0.1) is allowed")
    if parts.port is not None and parts.port != 443:
        raise NetError(NetErrorCode.BLOCKED, "only the default https port is allowed")
    host = check_host(parts.hostname, NetPolicy(allowed_hosts=allowed_hosts))
    return f"https://{host}"


def _is_loopback(base: str) -> bool:
    host = urlsplit(base).hostname or ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ModelClient:
    """Asks one OpenAI-compatible server for one chat completion at a time."""

    def __init__(  # noqa: PLR0913  # each is a separate, explicit knob
        self,
        base_url: str,
        *,
        allowed_hosts: frozenset[str] = frozenset(),
        api_key: SecretStr | None = None,
        access: AccessSource | None = None,
        model_key: KeySource | None = None,
        timeout_s: float = 180.0,
        max_response_bytes: int = 1024 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = check_model_url(base_url, allowed_hosts)
        if (api_key is not None or access is not None or model_key is not None) and _is_loopback(self._base):
            raise NetError(NetErrorCode.BLOCKED, "a key is never sent over plain http")
        if api_key is not None and model_key is not None:
            raise NetError(NetErrorCode.BLOCKED, "the model key comes from one place only")
        self._access = access
        self._model_key = model_key
        self._limit = max_response_bytes
        self._deadline_s = timeout_s  # the whole call; httpx's own timeout is per read
        headers = {"Accept": "application/json"}
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key.get_secret_value()}"
        self._http = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(timeout_s, connect=5.0),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._log = get_logger("agentzaaheen.net.model", host=urlsplit(self._base).hostname)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the connection pool."""
        await self._http.aclose()

    async def chat(
        self, messages: Sequence[Mapping[str, str]], *, max_tokens: int, temperature: float
    ) -> str:
        """Send ``messages``; return the assistant's text. Failures carry ``POST /v1/chat/completions``."""
        body = {
            "model": "local",
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        host = urlsplit(self._base).hostname or ""
        started = time.monotonic()
        try:
            with span(f"POST {host}", kind="client", attributes={"server.address": host}):
                answer = await self._ask(body)
                say(self._log, "model_answered", duration_ms=elapsed_ms(started))
                return answer
        except NetError as exc:
            say(self._log, "model_call_failed", code=exc.net_code.value, status=exc.status)
            raise exc.at(_OPERATION, 1, 1) from None

    async def _ask(self, body: dict[str, object]) -> str:
        headers = await self._access_headers() | await self._key_header()
        try:
            url = self._base + CHAT_PATH
            async with (
                asyncio.timeout(self._deadline_s),
                self._http.stream("POST", url, json=body, headers=headers) as response,
            ):
                refuse_status(response.status_code)
                if response.headers.get("Content-Type", "").startswith(STREAM_TYPE):
                    return await _read_stream(response, self._limit)
                raw = await read_capped(response, self._limit)
        except (httpx.TimeoutException, TimeoutError):
            raise NetError(NetErrorCode.TIMEOUT, "the model took too long") from None
        except httpx.TransportError:
            raise NetError(NetErrorCode.UNAVAILABLE, "could not reach the model server") from None
        return _first_answer(decode_object(raw))

    async def _access_headers(self) -> dict[str, str]:
        """Cloudflare's two headers for this one call, or none without ``access``."""
        if self._access is None:
            return {}
        try:
            token = await self._access()
        except SourceError:
            raise NetError(NetErrorCode.AUTH, "the server access token could not be opened") from None
        return {
            ACCESS_ID_HEADER: token.client_id,
            ACCESS_SECRET_HEADER: token.client_secret.get_secret_value(),
        }

    async def _key_header(self) -> dict[str, str]:
        """The model's own key for this one call, or none without ``model_key``."""
        if self._model_key is None:
            return {}
        try:
            key = await self._model_key()
        except SourceError:
            raise NetError(NetErrorCode.AUTH, "the server model key could not be opened") from None
        return {"Authorization": f"Bearer {key.get_secret_value()}"}


async def _read_stream(response: httpx.Response, limit: int) -> str:
    """Join a streamed answer's pieces; refuse it if it is over ``limit`` bytes or never says ``[DONE]``."""
    pieces: list[str] = []
    pending = b""
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise NetError(NetErrorCode.TOO_LARGE, "the response is over the size cap", status=200)
        *lines, pending = (pending + chunk).split(b"\n")
        for line in lines:
            if _take_line(line.strip(), pieces):
                return "".join(pieces)
    if _take_line(pending.strip(), pieces):
        return "".join(pieces)
    raise NetError(NetErrorCode.BAD_RESPONSE, "the answer stopped before it finished", status=200)


def _take_line(line: bytes, pieces: list[str]) -> bool:
    """Add one stream line's text to ``pieces``; ``True`` once the stream says ``[DONE]``.

    Blank lines end an event, ``:`` lines are pings, and lines other than
    ``data:`` (``event:``, ``id:``) carry nothing we use.
    """
    if not line.startswith(_DATA):
        return False
    data = line.removeprefix(_DATA).strip()
    if data == _DONE:
        return True
    try:
        event = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise NetError(NetErrorCode.BAD_RESPONSE, "a piece of the answer is not JSON", status=200) from None
    if not isinstance(event, dict):
        raise NetError(NetErrorCode.BAD_RESPONSE, "a piece of the answer is not a JSON object", status=200)
    if "error" in event:
        raise NetError(NetErrorCode.SERVER_ERROR, "the model stopped with an error", status=200)
    choices = event.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        delta = choices[0].get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            pieces.append(str(delta["content"]))
    return False  # a role, progress, reasoning or finish piece has no answer text


def _first_answer(payload: dict[str, object]) -> str:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return str(message["content"])
    raise NetError(NetErrorCode.BAD_RESPONSE, "the answer has no message text", status=200)

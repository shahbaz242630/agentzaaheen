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

Redirects are refused, the answer is size-capped and must be a JSON object,
and a failure carries a code, never the prompt, the answer or a key. A model
call is not retried: on a CPU it can take a minute, and the person asking can
simply ask again.
"""

from __future__ import annotations

import ipaddress
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
        timeout_s: float = 180.0,
        max_response_bytes: int = 1024 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = check_model_url(base_url, allowed_hosts)
        if (api_key is not None or access is not None) and _is_loopback(self._base):
            raise NetError(NetErrorCode.BLOCKED, "a key is never sent over plain http")
        self._access = access
        self._limit = max_response_bytes
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
            "stream": False,
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
        headers = await self._access_headers()
        try:
            url = self._base + CHAT_PATH
            async with self._http.stream("POST", url, json=body, headers=headers) as response:
                refuse_status(response.status_code)
                raw = await read_capped(response, self._limit)
        except httpx.TimeoutException:
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


def _first_answer(payload: dict[str, object]) -> str:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return str(message["content"])
    raise NetError(NetErrorCode.BAD_RESPONSE, "the answer has no message text", status=200)

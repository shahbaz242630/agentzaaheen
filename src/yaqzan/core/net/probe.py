"""Uptime probes: one GET that reads only the status line, to an allowlisted address (F03 slice 4).

The same rules as the JSON client, checked before a byte leaves: https only,
default port, a host on this probe's allowlist that is a name (never an IP
literal), no credentials, no query, a plain path. Redirects are reported, not
followed. The connection is closed as soon as the status line arrives: the
page is never read, so nothing a server sends can reach Yaqzan. No retries:
one failed answer is what the card should show. Implements the SDK's
``StatusGetter``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit

import httpx

from yaqzan.core.net.client import NetPolicy, check_host, check_path
from yaqzan.core.net.errors import NetError, NetErrorCode
from yaqzan.core.sdk import StatusAnswer, span

USER_AGENT = "Yaqzan-uptime/1"
DEFAULT_TIMEOUT_S = 10.0


def check_probe_url(url: str, policy: NetPolicy) -> str:
    """Return ``url`` if it is a plain https address on the allowlist, else raise ``NetError(BLOCKED)``."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise NetError(NetErrorCode.BLOCKED, "only https is allowed")
    if parts.username is not None or parts.password is not None:
        raise NetError(NetErrorCode.BLOCKED, "credentials are never put in a url")
    if parts.query or parts.fragment:
        raise NetError(NetErrorCode.BLOCKED, "an uptime address carries no query or fragment")
    if parts.port is not None and parts.port != 443:
        raise NetError(NetErrorCode.BLOCKED, "only the default https port is allowed")
    host = check_host(parts.hostname, policy)
    path = check_path(parts.path or "/")
    return f"https://{host}{path}"


class StatusProbe:
    """Status-only GETs to the allowlisted addresses. Implements the SDK's ``StatusGetter``."""

    def __init__(
        self,
        allowed_hosts: frozenset[str],
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._policy = NetPolicy(
            allowed_hosts=allowed_hosts, read_timeout_s=timeout_s, total_timeout_s=timeout_s
        )
        self._clock = clock
        self._http = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, self._policy.connect_timeout_s)),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the connection pool."""
        await self._http.aclose()

    async def status_of(self, url: str) -> StatusAnswer:
        """The status code and how long it took; the page itself is never read."""
        checked = check_probe_url(url, self._policy)
        host = urlsplit(checked).hostname or ""
        attributes = {"server.address": host, "http.request.method": "GET"}
        with span(f"probe {host}", kind="client", attributes=attributes) as current:
            started = self._clock()
            try:
                async with asyncio.timeout(self._policy.total_timeout_s):
                    async with self._http.stream("GET", checked) as response:
                        status = response.status_code
            except (TimeoutError, httpx.TimeoutException):
                raise NetError(NetErrorCode.TIMEOUT, "the address did not answer in time") from None
            except httpx.TransportError:
                raise NetError(NetErrorCode.UNAVAILABLE, "could not reach the address") from None
            elapsed_ms = max(0, round((self._clock() - started) * 1000))
            current.set_attribute("http.response.status_code", status)
            current.set_attribute("yaqzan.elapsed_ms", elapsed_ms)
        return StatusAnswer(status=status, elapsed_ms=elapsed_ms)

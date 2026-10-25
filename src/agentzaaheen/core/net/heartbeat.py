"""Heartbeats: one GET to a Healthchecks ping address, saying "this job ran" (F17 slice 6 follow-up).

The morning briefing pings its own Healthchecks check each time it reaches
the Chairman's phone. If no ping arrives by about 07:30 Dubai time (the
laptop was off, the dashboard was not running, or the send failed all
morning), Healthchecks alerts his phone itself, so a missing briefing is
never silent.

The ping address is a secret (whoever knows it can mark the check as up), so
it is a ``SecretStr`` and never appears in an error, a span or a log line.
Only ``https://hc-ping.com/<check uuid>`` is accepted. The answer (a short
"OK") is ignored. No redirects, no retries: a failed ping is reported, and
Healthchecks raising its own alert is the safe result.
"""

from __future__ import annotations

import re
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from agentzaaheen.core.net.client import NetPolicy, check_host
from agentzaaheen.core.net.errors import NetError, NetErrorCode
from agentzaaheen.core.sdk import get_logger, say, span

HEALTHCHECKS_PING_HOST = "hc-ping.com"
USER_AGENT = "AgentZaaheen-heartbeat/1"
_UUID_PATH = re.compile(r"^/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_OK = range(200, 300)


def check_ping_url(url: SecretStr, policy: NetPolicy) -> str:
    """Return the ping address if it is ``https://<allowlisted host>/<uuid>``, else raise ``NetError``.

    The error never quotes the address.
    """
    parts = urlsplit(url.get_secret_value().strip())
    if parts.scheme != "https":
        raise NetError(NetErrorCode.BLOCKED, "only https is allowed")
    if parts.username is not None or parts.password is not None:
        raise NetError(NetErrorCode.BLOCKED, "credentials are never put in a url")
    if parts.query or parts.fragment:
        raise NetError(NetErrorCode.BLOCKED, "a ping address carries no query or fragment")
    if parts.port is not None and parts.port != 443:
        raise NetError(NetErrorCode.BLOCKED, "only the default https port is allowed")
    host = check_host(parts.hostname, policy)
    if not _UUID_PATH.fullmatch(parts.path.lower()):
        raise NetError(NetErrorCode.BLOCKED, "a ping address is the check's uuid and nothing else")
    return f"https://{host}{parts.path.lower()}"


class HeartbeatClient:
    """Pings one secret Healthchecks address."""

    def __init__(
        self,
        url: SecretStr,
        *,
        allowed_hosts: frozenset[str] = frozenset({HEALTHCHECKS_PING_HOST}),
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        policy = NetPolicy(allowed_hosts=allowed_hosts)
        self._url = SecretStr(check_ping_url(url, policy))
        self._host = urlsplit(self._url.get_secret_value()).hostname or ""
        self._http = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._log = get_logger("agentzaaheen.heartbeat", host=self._host)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the connection pool."""
        await self._http.aclose()

    async def ping(self, source: str) -> None:
        """Say the job ``source`` (our own name: ``"briefing"``) ran. Raises ``NetError`` if it failed."""
        attributes = {"server.address": self._host, "http.request.method": "GET"}  # never the address
        with span(f"GET {self._host}", kind="client", attributes=attributes) as current:
            try:
                response = await self._http.get(self._url.get_secret_value())
            except httpx.TimeoutException:
                say(
                    self._log,
                    "heartbeat_failed",
                    source=source,
                    http_status=None,
                    code=NetErrorCode.TIMEOUT.value,
                )
                raise NetError(NetErrorCode.TIMEOUT, "the heartbeat service did not answer in time") from None
            except httpx.TransportError:
                say(
                    self._log,
                    "heartbeat_failed",
                    source=source,
                    http_status=None,
                    code=NetErrorCode.UNAVAILABLE.value,
                )
                raise NetError(NetErrorCode.UNAVAILABLE, "could not reach the heartbeat service") from None
            status = response.status_code
            current.set_attribute("http.response.status_code", status)
            if status not in _OK:
                say(
                    self._log,
                    "heartbeat_failed",
                    source=source,
                    http_status=status,
                    code=NetErrorCode.CLIENT_ERROR.value,
                )
                raise NetError(NetErrorCode.CLIENT_ERROR, "the heartbeat service refused it", status=status)
        say(self._log, "heartbeat_sent", source=source)

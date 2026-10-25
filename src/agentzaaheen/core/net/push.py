"""Push messages to the Chairman's phone through ntfy (ADR-031): the core's only outbound message.

One POST of plain text to ``https://<allowlisted host>/<topic>``. The topic is
a secret (whoever knows it can read the messages), so it is a ``SecretStr``
and never appears in a url we log, an error or a log line. The text and
title must already be built from a fixed template (``core.notify``); this
client only checks their size and that they are single, printable lines
(an alert), or several printable lines under :data:`MAX_LINES_BYTES` (the
morning briefing, :meth:`PushClient.send_lines`; ntfy's own limit is 4096).
No redirects, no retries: a failed send is reported, never repeated.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from types import TracebackType
from typing import Self

import httpx
from pydantic import SecretStr

from agentzaaheen.core.net.client import NetPolicy, check_host
from agentzaaheen.core.net.errors import NetError, NetErrorCode
from agentzaaheen.core.sdk import get_logger, say, span

NTFY_HOST = "ntfy.sh"
MAX_MESSAGE_BYTES = 1024
MAX_LINES_BYTES = 3584
MAX_TITLE = 120
_TOPIC = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_PRIORITY = frozenset({"default", "high"})
_OK = range(200, 300)


def _plain_line(text: str, limit: int) -> str:
    if not text or len(text.encode()) > limit or not text.isprintable():
        raise NetError(NetErrorCode.BLOCKED, "a push message must be one short printable line")
    return text


class PushClient:
    """Sends short alerts to one secret ntfy topic."""

    def __init__(
        self,
        topic: SecretStr,
        *,
        host: str = NTFY_HOST,
        allowed_hosts: frozenset[str] = frozenset({NTFY_HOST}),
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not _TOPIC.fullmatch(topic.get_secret_value()):
            raise NetError(NetErrorCode.BLOCKED, "the ntfy topic must be 16-64 letters, digits, - or _")
        self._host = check_host(host, NetPolicy(allowed_hosts=allowed_hosts))
        self._topic = topic
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s), follow_redirects=False, trust_env=False, transport=transport
        )
        self._log = get_logger("agentzaaheen.push", host=self._host)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the connection pool."""
        await self._http.aclose()

    async def send(self, title: str, message: str, *, priority: str = "default") -> None:
        """Send one alert. Raises ``NetError`` if it was refused or could not be sent."""
        await self._post(title, _plain_line(message, MAX_MESSAGE_BYTES), priority)

    async def send_lines(self, title: str, lines: Sequence[str], *, priority: str = "default") -> None:
        """Send one message of several printable lines (the morning briefing). Raises ``NetError``."""
        body = "\n".join(_plain_line(line, MAX_LINES_BYTES) for line in lines)
        if not lines or len(body.encode()) > MAX_LINES_BYTES:
            raise NetError(NetErrorCode.BLOCKED, "a push message must be a few short printable lines")
        await self._post(title, body, priority)

    async def _post(self, title: str, message: str, priority: str) -> None:
        if priority not in _PRIORITY:
            raise NetError(NetErrorCode.BLOCKED, "unknown push priority")
        headers = {"Title": _plain_line(title, MAX_TITLE), "Priority": priority, "Content-Type": "text/plain"}
        body = message.encode()
        url = f"https://{self._host}/{self._topic.get_secret_value()}"
        attributes = {"server.address": self._host, "http.request.method": "POST"}  # never the topic
        with span(f"POST {self._host}", kind="client", attributes=attributes) as current:
            try:
                response = await self._http.post(url, content=body, headers=headers)
            except httpx.TimeoutException:
                say(self._log, "push_failed", http_status=None, code=NetErrorCode.TIMEOUT.value)
                raise NetError(NetErrorCode.TIMEOUT, "the push service did not answer in time") from None
            except httpx.TransportError:
                say(self._log, "push_failed", http_status=None, code=NetErrorCode.UNAVAILABLE.value)
                raise NetError(NetErrorCode.UNAVAILABLE, "could not reach the push service") from None
            status = response.status_code
            current.set_attribute("http.response.status_code", status)
            if status not in _OK:
                say(self._log, "push_failed", http_status=status, code=NetErrorCode.CLIENT_ERROR.value)
                raise NetError(NetErrorCode.CLIENT_ERROR, "the push service refused it", status=status)
        say(self._log, "push_sent", bytes=len(body), priority=priority)

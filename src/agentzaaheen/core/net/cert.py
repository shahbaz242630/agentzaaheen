"""Certificate reads: one TLS handshake to an allowlisted name, reading only the certificate (F11 slice 5b).

The same host rules as every outbound call (a name on this probe's allowlist,
never an IP literal), port 443 only. The handshake checks the certificate the
way a browser would (trusted issuer, the right name, not expired); nothing is
sent after it and the connection is closed at once, so no page is ever asked
for. A certificate that is not valid is an answer (``cert.expired``,
``cert.wrong_name``, ``cert.invalid``), not an error; no answer at all raises.
IPv6 and IPv4 are tried side by side, so a name whose IPv6 address does not
answer still reads in time. Implements the SDK's ``CertGetter``.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from agentzaaheen.core.net.client import NetPolicy, check_host
from agentzaaheen.core.net.errors import NetError, NetErrorCode
from agentzaaheen.core.sdk import CertAnswer, get_logger, say, span

PORT = 443
DEFAULT_TIMEOUT_S = 10.0
_FAST_FALLBACK_S = 0.25  # start the next address (IPv4 after IPv6) if the first has not answered
_VERIFY_REASONS = {10: "cert.expired", 62: "cert.wrong_name"}  # OpenSSL's verify codes

# Opens one TLS connection and returns the peer certificate as Python's ssl module reports it.
Opener = Callable[[str, int, ssl.SSLContext, float], Awaitable[dict[str, object]]]


async def _open(host: str, port: int, context: ssl.SSLContext, timeout_s: float) -> dict[str, object]:
    _, writer = await asyncio.wait_for(
        asyncio.open_connection(
            host, port, ssl=context, server_hostname=host, happy_eyeballs_delay=_FAST_FALLBACK_S
        ),
        timeout_s,
    )
    try:
        cert = writer.get_extra_info("peercert")
    finally:
        writer.close()
        with contextlib.suppress(OSError, ssl.SSLError):
            await writer.wait_closed()
    return dict(cert) if isinstance(cert, dict) else {}


class CertProbe:
    """Reads the certificate of each allowlisted name. Implements the SDK's ``CertGetter``."""

    def __init__(
        self,
        allowed_hosts: frozenset[str],
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        context: ssl.SSLContext | None = None,
        port: int = PORT,
        opener: Opener = _open,
    ) -> None:
        self._policy = NetPolicy(allowed_hosts=allowed_hosts)
        self._timeout_s = timeout_s
        self._context = context if context is not None else ssl.create_default_context()
        self._port = port  # 443; a test server elsewhere only
        self._opener = opener

    async def certificate_of(self, host: str) -> CertAnswer:
        """Whether ``host``'s certificate is valid, and when it ends."""
        checked = check_host(host, self._policy)
        log = get_logger("agentzaaheen.net.cert", host=checked)
        with span(f"certificate {checked}", kind="client", attributes={"server.address": checked}):
            try:
                answer = await self._read(checked)
            except NetError as exc:
                say(log, "cert_failed", host=checked, code=exc.net_code.value)
                raise
            say(log, "cert_answered", host=checked, reason=answer.reason)
        return answer

    async def _read(self, host: str) -> CertAnswer:
        try:
            cert = await self._opener(host, self._port, self._context, self._timeout_s)
        except ssl.SSLCertVerificationError as exc:
            return CertAnswer(reason=_VERIFY_REASONS.get(exc.verify_code, "cert.invalid"))
        except TimeoutError:
            raise NetError(NetErrorCode.TIMEOUT, "the name did not answer in time") from None
        except (OSError, ssl.SSLError):
            raise NetError(NetErrorCode.UNAVAILABLE, "could not reach the name") from None
        ends = cert.get("notAfter")
        if not isinstance(ends, str):
            raise NetError(NetErrorCode.BAD_RESPONSE, "the certificate has no end date")
        try:
            not_after = datetime.fromtimestamp(ssl.cert_time_to_seconds(ends), UTC)
        except ValueError:
            raise NetError(NetErrorCode.BAD_RESPONSE, "the certificate's end date is not a date") from None
        return CertAnswer(reason="cert.ok", not_after=not_after)

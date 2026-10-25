"""Sends traces, metrics and logs over OTLP/HTTP as JSON (ADR-032): the core's only telemetry POST.

One kind of host only, matched as a whole name (Grafana Cloud's OTLP gateway),
https on the default port, no credentials or query in the url, no redirects and
no retries: telemetry is dropped rather than slow Agent Zaaheen down or pile up. The
token is a ``SecretStr`` that only ever becomes the ``Authorization`` header.
Synchronous on purpose: the telemetry workers call it from their own threads.
It never opens a span itself (a span about sending spans would loop), and its
own ``telemetry_dropped`` lines are never shipped (telemetry skips them).
"""

from __future__ import annotations

import base64
import re
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from yaqzan.core.net.client import check_path
from yaqzan.core.net.errors import NetError, NetErrorCode
from yaqzan.core.sdk import get_logger, say

OTLP_HOST = re.compile(r"otlp-gateway-prod-[a-z0-9-]{1,40}\.grafana\.net")
TRACES_PATH = "/v1/traces"
METRICS_PATH = "/v1/metrics"
LOGS_PATH = "/v1/logs"
MAX_BODY_BYTES = 4 * 1024 * 1024
_USER = re.compile(r"^[0-9]{1,12}$")
_OK = range(200, 300)


def check_otlp_endpoint(endpoint: str) -> str:
    """Return the cleaned endpoint (``https://<gateway>/otlp``), or raise ``NetError(BLOCKED)``."""
    parts = urlsplit(endpoint)
    if parts.scheme != "https" or parts.username is not None or parts.password is not None:
        raise NetError(NetErrorCode.BLOCKED, "the telemetry endpoint must be plain https")
    if parts.query or parts.fragment or parts.port not in {None, 443}:
        raise NetError(NetErrorCode.BLOCKED, "the telemetry endpoint carries no query, fragment or port")
    host = (parts.hostname or "").lower()
    if not OTLP_HOST.fullmatch(host):
        raise NetError(NetErrorCode.BLOCKED, "the telemetry endpoint is not an allowed gateway")
    path = check_path(parts.path.rstrip("/") or "/").rstrip("/")
    return f"https://{host}{path}"


class OtlpSender:
    """Posts encoded telemetry to one OTLP gateway. Implements the telemetry ``TelemetrySender``."""

    def __init__(
        self,
        endpoint: str,
        *,
        user: str,
        token: SecretStr,
        timeout_s: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        base = check_otlp_endpoint(endpoint)
        if not _USER.fullmatch(user):
            raise NetError(NetErrorCode.BLOCKED, "the telemetry user must be a numeric id")
        self._base = base
        basic = base64.b64encode(f"{user}:{token.get_secret_value()}".encode()).decode()
        self._http = httpx.Client(
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/json"},
            timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 5.0)),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._log = get_logger("yaqzan.net.otlp", host=urlsplit(base).hostname)

    def close(self) -> None:
        """Close the connection pool."""
        self._http.close()

    def send_traces(self, body: bytes) -> bool:
        """Post one batch of spans. True if the gateway took it."""
        return self._post(TRACES_PATH, body)

    def send_metrics(self, body: bytes) -> bool:
        """Post one batch of metrics. True if the gateway took it."""
        return self._post(METRICS_PATH, body)

    def send_logs(self, body: bytes) -> bool:
        """Post one batch of log lines. True if the gateway took it."""
        return self._post(LOGS_PATH, body)

    def _post(self, path: str, body: bytes) -> bool:
        # A refusal or failure is logged by code and dropped: never retried.
        signal = path.rsplit("/", 1)[-1]
        if len(body) > MAX_BODY_BYTES:
            say(self._log, "telemetry_dropped", kind=signal, reason="net.too_large", bytes=len(body))
            return False
        try:
            response = self._http.post(self._base + path, content=body)
        except httpx.TimeoutException:
            say(self._log, "telemetry_dropped", kind=signal, reason=NetErrorCode.TIMEOUT.value)
            return False
        except httpx.TransportError:
            say(self._log, "telemetry_dropped", kind=signal, reason=NetErrorCode.UNAVAILABLE.value)
            return False
        status = response.status_code
        if status not in _OK:
            say(
                self._log, "telemetry_dropped", kind=signal, reason=_refusal(status).value, http_status=status
            )
            return False
        return True


def _refusal(status: int) -> NetErrorCode:
    if status in {401, 403}:
        return NetErrorCode.AUTH
    if status == 429:  # the one "slow down" status
        return NetErrorCode.RATE_LIMITED
    return NetErrorCode.SERVER_ERROR if status >= 500 else NetErrorCode.CLIENT_ERROR

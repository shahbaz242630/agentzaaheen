"""The one outbound HTTP client: read-only JSON GETs to an allowlisted host.

Every rule is checked in code before a byte leaves (SECURITY NET-1):
https only, the host must be on this client's allowlist and must be a name,
never an IP literal; no credentials in the URL; redirects are refused, not
followed; responses are size-capped and must be a JSON object. Retries
(429, 5xx, timeouts, dropped connections) back off with full jitter and obey
``Retry-After``. Errors carry a code and status only, never a body or header.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from yaqzan.core.net.errors import NetError, NetErrorCode
from yaqzan.core.sdk import get_logger

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_AUTH_STATUSES = frozenset({401, 403})
_MAX_PATH = 512

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class NetPolicy:
    """Limits for one client. The defaults suit a vendor's read API."""

    allowed_hosts: frozenset[str]
    connect_timeout_s: float = 5.0
    read_timeout_s: float = 20.0
    total_timeout_s: float = 60.0
    max_response_bytes: int = 5 * 1024 * 1024
    max_attempts: int = 4
    base_delay_s: float = 0.5
    max_delay_s: float = 30.0


def check_host(host: str | None, policy: NetPolicy) -> str:
    """Return the lower-cased host if it is an allowlisted name (never an IP literal), else raise."""
    if not host:
        raise NetError(NetErrorCode.BLOCKED, "the url has no host")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise NetError(NetErrorCode.BLOCKED, "ip-literal hosts are not allowed")
    if host.lower() not in policy.allowed_hosts:
        raise NetError(NetErrorCode.BLOCKED, "the host is not on this client's allowlist")
    return host.lower()


def check_base_url(base_url: str, policy: NetPolicy) -> str:
    """Return the cleaned base url, or raise ``NetError(BLOCKED)``."""
    parts = urlsplit(base_url)
    if parts.scheme != "https":
        raise NetError(NetErrorCode.BLOCKED, "only https is allowed")
    if parts.username is not None or parts.password is not None:
        raise NetError(NetErrorCode.BLOCKED, "credentials are never put in a url")
    if parts.query or parts.fragment:
        raise NetError(NetErrorCode.BLOCKED, "a base url carries no query or fragment")
    if parts.port is not None and parts.port != 443:
        raise NetError(NetErrorCode.BLOCKED, "only the default https port is allowed")
    host = check_host(parts.hostname, policy)
    return f"https://{host}{parts.path.rstrip('/')}"


def check_path(path: str) -> str:
    """Return ``path`` if it is a plain absolute path, else raise ``NetError(BLOCKED)``."""
    bad = (
        not path.startswith("/")
        or path.startswith("//")
        or len(path) > _MAX_PATH
        or any(ch in path for ch in "?#\\@ \t\r\n")
        or ".." in path.split("/")
        or "://" in path
    )
    if bad:
        raise NetError(NetErrorCode.BLOCKED, "the path is not a plain absolute path")
    return path


class JsonClient:
    """Read-only JSON GETs against one base url. Implements the SDK's ``JsonGetter``.

    ``secret_headers`` (e.g. ``Authorization``) are only ever placed in the
    request headers; they never appear in errors or log lines.
    """

    def __init__(  # noqa: PLR0913  # each is a separate, explicit knob
        self,
        base_url: str,
        *,
        policy: NetPolicy,
        headers: Mapping[str, str] | None = None,
        secret_headers: Mapping[str, SecretStr] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._base = check_base_url(base_url, policy)
        self._policy = policy
        self._sleep = sleep
        self._jitter = jitter
        all_headers = {"Accept": "application/json", **(headers or {})}
        for name, value in (secret_headers or {}).items():
            all_headers[name] = value.get_secret_value()
        self._http = httpx.AsyncClient(
            headers=all_headers,
            timeout=httpx.Timeout(policy.read_timeout_s, connect=policy.connect_timeout_s),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._log = get_logger("yaqzan.net", host=urlsplit(self._base).hostname)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the connection pool."""
        await self._http.aclose()

    async def get_json(self, path: str, params: Mapping[str, str]) -> dict[str, object]:
        """GET ``path`` with ``params``; return the JSON object, retrying where it is safe.

        A failure carries ``GET <path>`` and the attempt it gave up on (``NetError.at``).
        """
        try:
            url = self._base + check_path(path)
        except NetError as exc:
            raise exc.at("GET (refused path)") from None  # never echo a path we refused
        attempt = 0
        try:
            check_host(urlsplit(url).hostname, self._policy)
            for attempt in range(1, self._policy.max_attempts + 1):
                last = attempt == self._policy.max_attempts
                try:
                    async with asyncio.timeout(self._policy.total_timeout_s):
                        outcome = await self._attempt(url, params)
                except TimeoutError:  # our own whole-response deadline, not httpx's per-read one
                    if last:
                        raise NetError(NetErrorCode.TIMEOUT, "the response took too long in total") from None
                    await self._back_off(attempt, None, path, NetErrorCode.TIMEOUT.value)
                    continue
                except NetError as exc:
                    if last or exc.net_code not in {NetErrorCode.TIMEOUT, NetErrorCode.UNAVAILABLE}:
                        raise
                    await self._back_off(attempt, None, path, exc.net_code.value)
                    continue
                if isinstance(outcome, dict):
                    return outcome
                status, retry_after = outcome
                if last:
                    code = NetErrorCode.RATE_LIMITED if status == 429 else NetErrorCode.SERVER_ERROR
                    raise NetError(code, "gave up after retries", status=status)
                await self._back_off(attempt, retry_after, path, f"http_{status}")
        except NetError as exc:
            exc.at(f"GET {path}", attempt or None, self._policy.max_attempts)
            raise
        raise AssertionError("unreachable: the last attempt always returns or raises")  # pragma: no cover

    async def _attempt(
        self, url: str, params: Mapping[str, str]
    ) -> dict[str, object] | tuple[int, str | None]:
        """One request: the decoded object, or ``(status, Retry-After)`` when a retry may help."""
        try:
            async with self._http.stream("GET", url, params=dict(params)) as response:
                status = response.status_code
                if status in _RETRY_STATUSES:
                    return status, response.headers.get("Retry-After")
                self._refuse_status(status)
                body = await self._read_capped(response)
        except httpx.TimeoutException:
            raise NetError(NetErrorCode.TIMEOUT, "the request timed out") from None
        except httpx.TransportError:
            raise NetError(NetErrorCode.UNAVAILABLE, "could not reach the host") from None
        return _decode(body)

    @staticmethod
    def _refuse_status(status: int) -> None:
        refuse_status(status)

    async def _read_capped(self, response: httpx.Response) -> bytes:
        return await read_capped(response, self._policy.max_response_bytes)

    async def _back_off(self, attempt: int, retry_after: str | None, path: str, reason: str) -> None:
        delay = self._jitter() * min(self._policy.max_delay_s, self._policy.base_delay_s * 2 ** (attempt - 1))
        if retry_after is not None and retry_after.strip().isdigit():
            delay = max(delay, float(retry_after.strip()))
        delay = min(delay, self._policy.max_delay_s)
        self._log.warning("net_retry", path=path, attempt=attempt, reason=reason, delay_s=round(delay, 2))
        await self._sleep(delay)


def refuse_status(status: int) -> None:
    """Raise unless ``status`` is a plain 200: redirects, auth refusals and errors all stop here."""
    if 300 <= status < 400:  # the 3xx range
        raise NetError(NetErrorCode.BLOCKED, "redirects are never followed", status=status)
    if status in _AUTH_STATUSES:
        raise NetError(NetErrorCode.AUTH, "the credential was refused", status=status)
    if status != 200:  # only a plain OK carries a body we trust
        raise NetError(NetErrorCode.CLIENT_ERROR, "the request was refused", status=status)


async def read_capped(response: httpx.Response, limit: int) -> bytes:
    """The body, refused as soon as it is (or says it will be) over ``limit`` bytes."""
    declared = response.headers.get("Content-Length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise NetError(NetErrorCode.TOO_LARGE, "the response is over the size cap", status=200)
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise NetError(NetErrorCode.TOO_LARGE, "the response is over the size cap", status=200)
        chunks.append(chunk)
    return b"".join(chunks)


def decode_object(body: bytes) -> dict[str, object]:
    """The body as a JSON object, or ``NetError(BAD_RESPONSE)``."""
    return _decode(body)


def _decode(body: bytes) -> dict[str, object]:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise NetError(NetErrorCode.BAD_RESPONSE, "the response is not JSON", status=200) from None
    if not isinstance(value, dict):
        raise NetError(NetErrorCode.BAD_RESPONSE, "the response is not a JSON object", status=200)
    return value

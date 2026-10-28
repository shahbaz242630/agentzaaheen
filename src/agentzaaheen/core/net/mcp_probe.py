"""The one test of a custom MCP server: connect, list its tools, stop (ADR-041 slice 1).

Nothing here ever calls a tool. :func:`list_tools` sends exactly three things
to the address the Chairman added: the MCP ``initialize`` handshake (our
client name and nothing else), the ``initialized`` notice, and ``tools/list``
(again for the next page, at most :data:`MAX_PAGES`). No key, header, cookie
or text of ours goes with them.

Before a byte leaves, :func:`check_mcp_url` and the address check refuse:
plain http, a user or password in the address, a query or fragment (where
keys hide), a port other than 443, an IP literal, and a name that resolves
to a private, loopback, link-local or otherwise non-public address (so a
custom server can never be used to reach inside this machine or its network).

The whole test stops after :data:`DEADLINE_S` seconds; every reply is
size-capped; redirects are refused, never followed; proxy settings from the
environment are ignored. What comes back is someone else's text: tool names
are checked, descriptions trimmed and stripped of control characters, and
the caller must show them as untrusted. Failures carry a code and status
only, never the server's own error text.

Known limit: the name is resolved once for the check and again by the
connection, so a server that changes its DNS answer in between could still
be reached. The test sends nothing of ours and returns only a tool list to
the Chairman, so this is accepted for slice 1 (noted in ADR-041).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from agentzaaheen.core.net.errors import NetError, NetErrorCode
from agentzaaheen.core.sdk import elapsed_ms, get_logger, say, span

PROTOCOL_VERSION = "2025-06-18"
DEADLINE_S = 8.0
MAX_REPLY_BYTES = 2 * 1024 * 1024  # per reply; one large tool list is ~1 MB
MAX_PAGES = 10
MAX_TOOLS = 500
MAX_DESCRIPTION = 2000
MAX_URL = 512
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.\-/]{1,128}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa", ".intranet", ".corp")
_CLIENT_INFO = {"name": "agentzaaheen", "version": "1"}

Resolver = Callable[[str], Awaitable[Sequence[str]]]


@dataclass(frozen=True)
class McpTool:
    """One tool as the server describes it. Its words are the server's: untrusted."""

    name: str
    description: str


@dataclass(frozen=True)
class McpListing:
    """What the test found. ``fingerprint`` covers every tool's name, description and input shape."""

    server_name: str
    tools: tuple[McpTool, ...]
    skipped: int  # tools left out because their name was not usable
    fingerprint: str


def check_mcp_url(url: str) -> str:
    """Return the cleaned address, or raise ``NetError(BLOCKED)`` saying why in plain words."""
    if len(url) > MAX_URL:
        raise NetError(NetErrorCode.BLOCKED, "the address is too long")
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise NetError(NetErrorCode.BLOCKED, "only https addresses are allowed")
    if parts.username is not None or parts.password is not None:
        raise NetError(NetErrorCode.BLOCKED, "an address may not carry a user or password")
    if parts.query or parts.fragment:
        raise NetError(NetErrorCode.BLOCKED, "an address may not carry a query or fragment (keys hide there)")
    try:
        port = parts.port
    except ValueError:
        raise NetError(NetErrorCode.BLOCKED, "the address has a bad port") from None
    if port is not None and port != 443:  # the https port
        raise NetError(NetErrorCode.BLOCKED, "only the default https port is allowed")
    host = (parts.hostname or "").lower()
    if not host or "." not in host:
        raise NetError(NetErrorCode.BLOCKED, "the address needs a full public host name")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise NetError(NetErrorCode.BLOCKED, "an address must be a name, not an IP number")
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        raise NetError(NetErrorCode.BLOCKED, "only public addresses are allowed")
    path = parts.path or "/"
    if any(ch in path for ch in "\\ \t\r\n") or ".." in path.split("/"):
        raise NetError(NetErrorCode.BLOCKED, "the address has an odd path")
    return f"https://{host}{path}"


async def resolve(host: str) -> Sequence[str]:
    """Every address ``host`` resolves to (the real resolver; tests pass their own)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def _check_public(host: str, resolver: Resolver) -> None:
    try:
        addresses = await resolver(host)
    except OSError:
        raise NetError(NetErrorCode.UNAVAILABLE, "the name could not be found") from None
    if not addresses:
        raise NetError(NetErrorCode.UNAVAILABLE, "the name could not be found")
    for address in addresses:
        try:
            public = ipaddress.ip_address(address.split("%")[0]).is_global
        except ValueError:
            public = False
        if not public:
            raise NetError(NetErrorCode.BLOCKED, "the name points inside a private network")


async def list_tools(
    url: str,
    *,
    resolver: Resolver = resolve,
    transport: httpx.AsyncBaseTransport | None = None,
    deadline_s: float = DEADLINE_S,
) -> McpListing:
    """Connect to the MCP server at ``url``, list its tools, and stop. Never calls a tool."""
    address = check_mcp_url(url)
    host = urlsplit(address).hostname or ""
    log = get_logger("agentzaaheen.net.mcp_probe", host=host)
    started = time.monotonic()
    try:
        with span(f"POST {host}", kind="client", attributes={"server.address": host}):
            async with asyncio.timeout(deadline_s):
                await _check_public(host, resolver)
                listing = await _talk(address, transport)
    except TimeoutError:
        error = NetError(NetErrorCode.TIMEOUT, "the server did not finish answering in time")
        say(log, "mcp_test_failed", code=error.net_code.value, status=None, duration_ms=elapsed_ms(started))
        raise error from None
    except NetError as exc:
        say(
            log,
            "mcp_test_failed",
            code=exc.net_code.value,
            status=exc.status,
            duration_ms=elapsed_ms(started),
        )
        raise
    say(
        log,
        "mcp_tools_listed",
        tools=len(listing.tools),
        skipped=listing.skipped,
        duration_ms=elapsed_ms(started),
    )
    return listing


async def _talk(address: str, transport: httpx.AsyncBaseTransport | None) -> McpListing:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    async with httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(DEADLINE_S, connect=5.0),
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    ) as http:
        session: dict[str, str] = {}
        hello = await _ask(http, address, session, request_id=1, method="initialize", params=_hello())
        info = hello.get("serverInfo")
        server_name = str(info.get("name", ""))[:128] if isinstance(info, dict) else ""
        session["MCP-Protocol-Version"] = str(hello.get("protocolVersion", PROTOCOL_VERSION))[:32]
        await _notify(http, address, session)
        raw_tools: list[object] = []
        cursor: str | None = None
        for page in range(MAX_PAGES):
            params: dict[str, object] = {} if cursor is None else {"cursor": cursor}
            result = await _ask(
                http, address, session, request_id=2 + page, method="tools/list", params=params
            )
            tools = result.get("tools")
            if not isinstance(tools, list):
                raise NetError(NetErrorCode.BAD_RESPONSE, "the tool list is not a list", status=200)
            raw_tools.extend(tools)
            if len(raw_tools) > MAX_TOOLS:
                raise NetError(NetErrorCode.TOO_LARGE, "the server lists too many tools", status=200)
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor[:1024]
    return _listing(server_name, raw_tools)


def _hello() -> dict[str, object]:
    return {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": _CLIENT_INFO}


async def _ask(  # noqa: PLR0913  # the connection, then the one message
    http: httpx.AsyncClient,
    address: str,
    session: dict[str, str],
    *,
    request_id: int,
    method: str,
    params: dict[str, object],
) -> dict[str, object]:
    body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    try:
        async with http.stream("POST", address, json=body, headers=session) as response:
            _refuse(response.status_code)
            if method == "initialize" and (sid := response.headers.get("Mcp-Session-Id")):
                if len(sid) > 256 or not sid.isprintable():  # a sane session id
                    raise NetError(NetErrorCode.BAD_RESPONSE, "the session id is not usable", status=200)
                session["Mcp-Session-Id"] = sid
            kind = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if kind == "text/event-stream":
                message = await _from_stream(response, request_id)
            elif kind == "application/json":
                message = _decode(await _capped(response))
            else:
                raise NetError(NetErrorCode.BAD_RESPONSE, "the server did not answer as MCP", status=200)
    except httpx.TimeoutException:
        raise NetError(NetErrorCode.TIMEOUT, "the server took too long") from None
    except httpx.TransportError:
        raise NetError(NetErrorCode.UNAVAILABLE, "could not reach the server") from None
    return _result(message, request_id)


async def _notify(http: httpx.AsyncClient, address: str, session: dict[str, str]) -> None:
    body = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    try:
        async with http.stream("POST", address, json=body, headers=session) as response:
            if response.status_code not in {200, 202, 204}:
                _refuse(response.status_code)
    except httpx.TimeoutException:
        raise NetError(NetErrorCode.TIMEOUT, "the server took too long") from None
    except httpx.TransportError:
        raise NetError(NetErrorCode.UNAVAILABLE, "could not reach the server") from None


def _refuse(status: int) -> None:
    if status == 200:  # only a plain OK carries an answer
        return
    if 300 <= status < 400:  # the 3xx range
        raise NetError(NetErrorCode.BLOCKED, "redirects are never followed", status=status)
    if status in {401, 403}:
        raise NetError(NetErrorCode.AUTH, "the server wants a key", status=status)
    if status == 429:
        raise NetError(NetErrorCode.RATE_LIMITED, "the server is busy", status=status)
    if status >= 500:  # the server's own errors
        raise NetError(NetErrorCode.SERVER_ERROR, "the server had an error", status=status)
    raise NetError(NetErrorCode.CLIENT_ERROR, "the server refused the request", status=status)


async def _capped(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_REPLY_BYTES:
            raise NetError(NetErrorCode.TOO_LARGE, "the answer is over the size cap", status=200)
        chunks.append(chunk)
    return b"".join(chunks)


async def _from_stream(response: httpx.Response, request_id: int) -> object:
    """The JSON-RPC answer to ``request_id`` from an event stream; other events are skipped."""
    size = 0
    data: list[str] = []
    async for line in response.aiter_lines():
        size += len(line) + 1
        if size > MAX_REPLY_BYTES:
            raise NetError(NetErrorCode.TOO_LARGE, "the answer is over the size cap", status=200)
        if line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif line == "" and data:
            message = _decode("\n".join(data).encode())
            data = []
            if isinstance(message, dict) and message.get("id") == request_id:
                return message
    if data:
        message = _decode("\n".join(data).encode())
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise NetError(NetErrorCode.BAD_RESPONSE, "the server never answered the question", status=200)


def _decode(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise NetError(NetErrorCode.BAD_RESPONSE, "the answer is not JSON", status=200) from None


def _result(message: object, request_id: int) -> dict[str, object]:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or message.get("id") != request_id:
        raise NetError(NetErrorCode.BAD_RESPONSE, "the server did not answer as MCP", status=200)
    if "error" in message:
        raise NetError(NetErrorCode.CLIENT_ERROR, "the server answered with an error", status=200)
    result = message.get("result")
    if not isinstance(result, dict):
        raise NetError(NetErrorCode.BAD_RESPONSE, "the server did not answer as MCP", status=200)
    return result


def clean_text(text: str, limit: int) -> str:
    """Someone else's words, safe to keep and show as plain text: no control characters, trimmed."""
    return _CONTROL.sub("", text).strip()[:limit]


def _listing(server_name: str, raw_tools: list[object]) -> McpListing:
    tools: list[McpTool] = []
    printed: list[dict[str, object]] = []
    skipped = 0
    seen: set[str] = set()
    for raw in raw_tools:
        name = raw.get("name") if isinstance(raw, dict) else None
        if (
            not isinstance(raw, dict)
            or not isinstance(name, str)
            or not _TOOL_NAME.match(name)
            or name in seen
        ):
            skipped += 1
            continue
        seen.add(name)
        description = raw.get("description")
        text = clean_text(description, MAX_DESCRIPTION) if isinstance(description, str) else ""
        tools.append(McpTool(name=name, description=text))
        printed.append({"name": name, "description": description, "inputSchema": raw.get("inputSchema")})
    printed.sort(key=lambda tool: str(tool["name"]))
    canonical = json.dumps(printed, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return McpListing(
        server_name=clean_text(server_name, 128),
        tools=tuple(sorted(tools, key=lambda tool: tool.name)),
        skipped=skipped,
        fingerprint=hashlib.sha256(canonical.encode()).hexdigest(),
    )

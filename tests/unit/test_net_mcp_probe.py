"""The custom MCP test: handshake + tool list only, public https only, capped (ADR-041 slice 1).

Every test runs through ``httpx.MockTransport`` and a fake resolver: no real
network. The fake server records every message, so the tests can prove the
probe never calls a tool and never sends anything of ours.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence

import httpx
import pytest

from agentzaaheen.core.net import McpListing, NetError, NetErrorCode, check_mcp_url, list_tools
from agentzaaheen.core.net.mcp_probe import DEADLINE_S, Resolver

URL = "https://mcp.example.com/mcp"
TOOLS = [
    {"name": "search", "description": "Search the web.", "inputSchema": {"type": "object"}},
    {"name": "fetch", "description": "Fetch a page.\x07\x1b[31m", "inputSchema": {"type": "object"}},
]


async def public(_host: str) -> Sequence[str]:
    return ["93.184.216.34"]


class FakeServer:
    """A Streamable HTTP MCP server. ``stream``: answer as an event stream instead of plain JSON."""

    def __init__(
        self,
        tools: list[object] | None = None,
        *,
        stream: bool = False,
        status: int = 200,
        pages: int = 1,
        session: str | None = "s-1",
    ) -> None:
        self.tools = TOOLS if tools is None else tools
        self.stream = stream
        self.status = status
        self.pages = pages
        self.session = session
        self.seen: list[httpx.Request] = []

    def bodies(self) -> list[dict[str, object]]:
        return [json.loads(r.content) for r in self.seen]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if self.status != 200:
            return httpx.Response(self.status, headers={"Location": "https://elsewhere.example.com/"})
        body = json.loads(request.content)
        method = body.get("method")
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result: dict[str, object] = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "Example\x00 server", "version": "1"},
            }
            headers = {"Mcp-Session-Id": self.session} if self.session else {}
            return self.answer(body["id"], result, headers)
        if method == "tools/list":
            page = int((body.get("params") or {}).get("cursor", "0"))
            result = {"tools": self.tools if page == 0 else [{"name": f"more{page}", "description": ""}]}
            if page + 1 < self.pages:
                result["nextCursor"] = str(page + 1)
            return self.answer(body["id"], result, {})
        return httpx.Response(400)

    def answer(self, request_id: object, result: object, headers: dict[str, str]) -> httpx.Response:
        message = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})
        if self.stream:
            note = json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {}})
            text = f"event: message\ndata: {note}\n\nevent: message\ndata: {message}\n\n"
            return httpx.Response(200, text=text, headers={**headers, "Content-Type": "text/event-stream"})
        return httpx.Response(200, text=message, headers={**headers, "Content-Type": "application/json"})


def probe(
    server: FakeServer, url: str = URL, *, resolver: Resolver = public, deadline_s: float = DEADLINE_S
) -> McpListing:
    transport = httpx.MockTransport(server.handle)
    return asyncio.run(list_tools(url, resolver=resolver, transport=transport, deadline_s=deadline_s))


def failure(
    server: FakeServer, url: str = URL, *, resolver: Resolver = public, deadline_s: float = DEADLINE_S
) -> NetError:
    with pytest.raises(NetError) as caught:
        probe(server, url, resolver=resolver, deadline_s=deadline_s)
    return caught.value


@pytest.mark.parametrize("stream", [False, True])
def test_it_lists_tools_and_only_ever_says_hello_and_asks_for_the_list(stream: bool) -> None:
    server = FakeServer(stream=stream)
    listing = probe(server)
    assert [t.name for t in listing.tools] == ["fetch", "search"]
    assert listing.server_name == "Example server"
    methods = [b.get("method") for b in server.bodies()]
    assert methods == ["initialize", "notifications/initialized", "tools/list"]
    assert all(r.method == "POST" and str(r.url) == URL for r in server.seen)


def test_it_sends_nothing_of_ours_only_our_client_name() -> None:
    server = FakeServer()
    probe(server)
    hello = server.bodies()[0]
    assert hello["params"] == {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "agentzaaheen", "version": "1"},
    }
    for request in server.seen:
        names = {k.lower() for k in request.headers}
        assert not names & {"authorization", "cookie", "x-api-key"}
        assert request.headers.get("user-agent", "").startswith("python-httpx")  # httpx's own, nothing added


def test_the_session_id_and_protocol_version_go_back_after_the_hello() -> None:
    server = FakeServer()
    probe(server)
    first, *rest = server.seen
    assert "mcp-session-id" not in {k.lower() for k in first.headers}
    for request in rest:
        assert request.headers["Mcp-Session-Id"] == "s-1"
        assert request.headers["MCP-Protocol-Version"] == "2025-06-18"


def test_descriptions_lose_control_characters_and_names_must_be_usable() -> None:
    tools: list[object] = [
        *TOOLS,
        {"name": "bad name with spaces", "description": "x"},
        {"name": "search", "description": "a second search"},
        "not a tool",
        {"description": "no name"},
    ]
    listing = probe(FakeServer(tools))
    by_name = {t.name: t.description for t in listing.tools}
    assert by_name == {"fetch": "Fetch a page.[31m", "search": "Search the web."}
    assert listing.skipped == 4


def test_the_fingerprint_changes_when_a_description_or_input_shape_changes() -> None:
    base = probe(FakeServer()).fingerprint
    again = probe(FakeServer(list(reversed(TOOLS)))).fingerprint
    worded = probe(FakeServer([{**TOOLS[0], "description": "Search, then email it."}, TOOLS[1]])).fingerprint
    shaped = probe(
        FakeServer([{**TOOLS[0], "inputSchema": {"type": "object", "x": 1}}, TOOLS[1]])
    ).fingerprint
    assert base == again  # order does not matter
    assert len({base, worded, shaped}) == 3


def test_pages_are_followed_but_only_so_far() -> None:
    assert len(probe(FakeServer(pages=3)).tools) == 4
    server = FakeServer(pages=50)
    assert len(probe(server).tools) == 2 + 9
    assert [b.get("method") for b in server.bodies()].count("tools/list") == 10


def test_too_many_tools_is_refused() -> None:
    tools: list[object] = [{"name": f"t{i}", "description": ""} for i in range(501)]
    assert failure(FakeServer(tools)).net_code is NetErrorCode.TOO_LARGE


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, NetErrorCode.AUTH),
        (403, NetErrorCode.AUTH),
        (302, NetErrorCode.BLOCKED),
        (307, NetErrorCode.BLOCKED),
        (404, NetErrorCode.CLIENT_ERROR),
        (429, NetErrorCode.RATE_LIMITED),
        (500, NetErrorCode.SERVER_ERROR),
    ],
)
def test_refusals_say_why_and_a_redirect_is_never_followed(status: int, code: NetErrorCode) -> None:
    server = FakeServer(status=status)
    assert failure(server).net_code is code
    assert len(server.seen) == 1  # nothing further: not even the redirect's target


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.example.com/mcp",
        "https://user:pass@mcp.example.com/mcp",
        "https://mcp.example.com/mcp?key=abc",
        "https://mcp.example.com/mcp#x",
        "https://mcp.example.com:8443/mcp",
        "https://93.184.216.34/mcp",
        "https://[::1]/mcp",
        "https://localhost/mcp",
        "https://printer.local/mcp",
        "https://box.internal/mcp",
        "https://intranet/mcp",
        "https://mcp.example.com/../x",
        "ftp://mcp.example.com/",
        "https://mcp.example.com/" + "a" * 600,
    ],
)
def test_addresses_that_are_not_plain_public_https_are_refused_before_anything_is_sent(url: str) -> None:
    server = FakeServer()
    assert failure(server, url).net_code is NetErrorCode.BLOCKED
    assert server.seen == []


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "fd00::1"]
)
def test_a_name_that_points_inside_a_private_network_is_refused(address: str) -> None:
    async def inside(_host: str) -> Sequence[str]:
        return ["93.184.216.34", address]

    server = FakeServer()
    error = failure(server, resolver=inside)
    assert error.net_code is NetErrorCode.BLOCKED
    assert server.seen == []


def test_a_name_that_cannot_be_found_is_unavailable() -> None:
    async def missing(_host: str) -> Sequence[str]:
        raise OSError("no such name")

    assert failure(FakeServer(), resolver=missing).net_code is NetErrorCode.UNAVAILABLE


def test_the_whole_test_stops_at_the_deadline() -> None:
    async def slow(_host: str) -> Sequence[str]:
        await asyncio.sleep(5)
        return ["93.184.216.34"]

    assert failure(FakeServer(), resolver=slow, deadline_s=0.05).net_code is NetErrorCode.TIMEOUT


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(200, text="<html>hi</html>", headers={"Content-Type": "text/html"}),
        httpx.Response(200, text="not json", headers={"Content-Type": "application/json"}),
        httpx.Response(
            200, text='{"jsonrpc":"2.0","id":99,"result":{}}', headers={"Content-Type": "application/json"}
        ),
        httpx.Response(
            200, text='{"jsonrpc":"2.0","id":1,"result":[]}', headers={"Content-Type": "application/json"}
        ),
        httpx.Response(200, text="data: {}\n\n", headers={"Content-Type": "text/event-stream"}),
    ],
)
def test_an_answer_that_is_not_mcp_is_a_bad_response(reply: httpx.Response) -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return reply

    with pytest.raises(NetError) as caught:
        asyncio.run(list_tools(URL, resolver=public, transport=httpx.MockTransport(handle)))
    assert caught.value.net_code is NetErrorCode.BAD_RESPONSE


def test_an_error_answer_is_refused_without_its_words() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        text = json.dumps(
            {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -1, "message": "secret detail"}}
        )
        return httpx.Response(200, text=text, headers={"Content-Type": "application/json"})

    with pytest.raises(NetError) as caught:
        asyncio.run(list_tools(URL, resolver=public, transport=httpx.MockTransport(handle)))
    assert caught.value.net_code is NetErrorCode.CLIENT_ERROR
    assert "secret detail" not in str(caught.value)


def test_an_oversized_answer_is_refused() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"x" * (3 * 1024 * 1024), headers={"Content-Type": "application/json"}
        )

    with pytest.raises(NetError) as caught:
        asyncio.run(list_tools(URL, resolver=public, transport=httpx.MockTransport(handle)))
    assert caught.value.net_code is NetErrorCode.TOO_LARGE


def test_check_mcp_url_cleans_a_good_address_and_explains_a_bad_one() -> None:
    assert check_mcp_url(" https://MCP.Example.com/mcp ") == "https://mcp.example.com/mcp"
    assert check_mcp_url("https://mcp.example.com") == "https://mcp.example.com/"
    with pytest.raises(NetError) as caught:
        check_mcp_url("http://mcp.example.com/")
    assert caught.value.plain == "only https addresses are allowed"

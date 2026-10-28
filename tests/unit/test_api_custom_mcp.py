"""The MCP tab's routes: add as a request, test, remove; only from our own page (ADR-041 slice 1)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from agentzaaheen.core.api import SamplePortfolio, create_app
from agentzaaheen.core.api.app import REQUEST_HEADER
from agentzaaheen.core.policy import McpVerdict
from agentzaaheen.core.store import CustomMcpRow, McpTest, McpToolRow, StoreError, StoreErrorCode

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
OURS = {REQUEST_HEADER: "1", "Origin": "http://127.0.0.1"}
URL = "https://mcp.example.com/mcp"


class FakeMcp:
    """Keeps servers in memory, with the same verdicts as the real one for the cases tested here."""

    def __init__(self) -> None:
        self.rows: dict[str, CustomMcpRow] = {}
        self.broken = False
        self.tested: list[str] = []

    async def servers(self) -> list[CustomMcpRow]:
        if self.broken:
            raise StoreError(StoreErrorCode.UNAVAILABLE, "down")
        return list(self.rows.values())

    async def add(self, name: str, url: str) -> tuple[McpVerdict, str | None]:
        if url.startswith("http://"):
            return McpVerdict.BAD_ADDRESS, "only https addresses are allowed"
        if name == "github":
            return McpVerdict.RESERVED_NAME, None
        if name in self.rows:
            return McpVerdict.NAME_TAKEN, None
        self.rows[name] = CustomMcpRow(
            id=uuid4(),
            name=name,
            url=url,
            status="requested",
            added_by="user:chairman",
            added_at=NOW,
            test=None,
        )
        return McpVerdict.DONE, None

    async def test(self, name: str) -> McpVerdict:
        row = self.rows.get(name)
        if row is None:
            return McpVerdict.UNKNOWN_SERVER
        self.tested.append(name)
        found = McpTest(
            at=NOW,
            outcome="mcp.listed",
            server_name="Example",
            tools=(McpToolRow(name="search", description="<b>Search</b> the web."),),
            skipped=0,
            fingerprint="c" * 64,
        )
        self.rows[name] = CustomMcpRow(**{**row.__dict__, "test": found})
        return McpVerdict.DONE

    async def remove(self, name: str) -> McpVerdict:
        return McpVerdict.DONE if self.rows.pop(name, None) else McpVerdict.UNKNOWN_SERVER


def client(mcp: FakeMcp | None) -> TestClient:
    return TestClient(create_app(SamplePortfolio(NOW), custom_mcp=mcp), base_url="http://127.0.0.1")


def post(
    c: TestClient, path: str, body: object = None, headers: dict[str, str] | None = None
) -> httpx.Response:
    response: httpx.Response = c.post(
        path, json=body if body is not None else {}, headers=OURS if headers is None else headers
    )
    return response


def test_without_a_database_the_tab_says_servers_cannot_be_added_here() -> None:
    c = client(None)
    assert c.get("/api/mcp").json() == {"available": False, "servers": []}
    refused = post(c, "/api/mcp", {"name": "web", "url": URL})
    assert (refused.status_code, refused.json()) == (503, {"detail": "mcp.not_available"})


def test_add_test_remove() -> None:
    fake = FakeMcp()
    c = client(fake)
    added = post(c, "/api/mcp", {"name": "web", "url": URL})
    assert added.status_code == 200
    assert added.json()["servers"] == [
        {"name": "web", "url": URL, "status": "requested", "added_at": "2026-10-28T12:00:00Z", "test": None}
    ]
    assert fake.tested == []  # adding never tests
    tested = post(c, "/api/mcp/web/test").json()["servers"][0]["test"]
    assert tested["outcome"] == "mcp.listed"
    assert tested["tools"] == [
        {"name": "search", "description": "<b>Search</b> the web."}
    ]  # as text; the page escapes
    assert post(c, "/api/mcp/web/remove").json() == {"available": True, "servers": []}


def test_refusals_say_why() -> None:
    c = client(FakeMcp())
    post(c, "/api/mcp", {"name": "web", "url": URL})
    cases = [
        (post(c, "/api/mcp", {"name": "web", "url": URL}), 409, "mcp.name_taken"),
        (post(c, "/api/mcp", {"name": "github", "url": URL}), 422, "mcp.reserved_name"),
        (post(c, "/api/mcp/nobody/test"), 404, "mcp.unknown_server"),
        (post(c, "/api/mcp/nobody/remove"), 404, "mcp.unknown_server"),
    ]
    for response, status, detail in cases:
        assert (response.status_code, response.json()) == (status, {"detail": detail})
    bad = post(c, "/api/mcp", {"name": "x", "url": "http://mcp.example.com/"})
    assert (bad.status_code, bad.json()) == (
        422,
        {"detail": {"code": "mcp.bad_address", "why": "only https addresses are allowed"}},
    )


def test_odd_names_in_the_address_and_extra_fields_are_refused_before_anything_runs() -> None:
    fake = FakeMcp()
    c = client(fake)
    assert post(c, "/api/mcp/Web/test").status_code == 422
    assert post(c, "/api/mcp/" + "a" * 40 + "/test").status_code == 422
    assert post(c, "/api/mcp", {"name": "web", "url": URL, "key": "secret"}).status_code == 422
    assert (
        post(c, "/api/mcp", {"name": "web", "url": "https://x.example.com/" + "a" * 600}).status_code == 422
    )
    assert fake.rows == {}


def test_only_our_own_page_may_change_anything() -> None:
    fake = FakeMcp()
    c = client(fake)
    no_header = post(c, "/api/mcp", {"name": "web", "url": URL}, headers={})
    other_site = post(
        c, "/api/mcp", {"name": "web", "url": URL}, headers={**OURS, "Origin": "https://evil.example"}
    )
    assert (no_header.status_code, other_site.status_code) == (403, 403)
    assert fake.rows == {}


def test_a_database_failure_is_a_503_not_a_crash() -> None:
    fake = FakeMcp()
    fake.broken = True
    response = client(fake).get("/api/mcp")
    assert (response.status_code, response.json()) == (503, {"detail": "mcp.store_unavailable"})

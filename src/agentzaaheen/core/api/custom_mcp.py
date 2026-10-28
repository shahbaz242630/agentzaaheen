"""The Customize page's MCP tab: custom MCP servers added, tested, removed (ADR-041 slice 1).

``GET /api/mcp`` lists the servers added and what each one's last test found.
``POST /api/mcp`` adds one (a name and an https address) as a request: it is
not called and cannot be used. ``POST /api/mcp/{name}/test`` connects, lists
its tools and disconnects (8 s at most, none of our keys or text sent).
``POST /api/mcp/{name}/remove`` removes it. All as the Chairman on the
dashboard (no login yet). No Windows Hello: nothing here widens what any
model may use; approving a server (slice 2) will need it.

Tool names and descriptions are the server's own words: the page shows them
as plain text, marked as not checked.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol

from fastapi import Depends, FastAPI, HTTPException, Path
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from agentzaaheen.core.policy import Channel, CustomMcpControl, McpVerdict, Role
from agentzaaheen.core.sdk import get_logger, say
from agentzaaheen.core.store import CustomMcpRow, StoreError

_NAME = r"^[a-z][a-z0-9_-]{0,31}$"
_REFUSED_STATUS = {
    McpVerdict.NOT_ALLOWED: 403,
    McpVerdict.BAD_NAME: 422,
    McpVerdict.RESERVED_NAME: 422,
    McpVerdict.BAD_ADDRESS: 422,
    McpVerdict.NAME_TAKEN: 409,
    McpVerdict.UNKNOWN_SERVER: 404,
}


class _View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class McpToolView(_View):
    """One tool, in the server's own words (untrusted)."""

    name: str
    description: str


class McpTestView(_View):
    """The last test: when, what came of it (``mcp.listed`` or why not), and the tools it listed."""

    at: AwareDatetime
    outcome: str
    server_name: str | None = None
    tools: tuple[McpToolView, ...] | None = None
    skipped: int | None = None
    fingerprint: str | None = None


class McpServerView(_View):
    """One custom server. ``status`` is always ``requested`` in slice 1: nothing can use it."""

    name: str
    url: str
    status: str
    added_at: AwareDatetime
    test: McpTestView | None = None


class McpPage(_View):
    """The MCP tab. ``available``: servers can be added here (a database is connected)."""

    available: bool
    servers: tuple[McpServerView, ...] = ()


class McpAdd(_View):
    """What the page sends to add a server."""

    name: str = Field(max_length=64)
    url: str = Field(max_length=512)


def server_view(row: CustomMcpRow) -> McpServerView:
    """The page's view of one stored server."""
    test = None
    if row.test is not None:
        tools = row.test.tools
        test = McpTestView(
            at=row.test.at,
            outcome=row.test.outcome,
            server_name=row.test.server_name,
            tools=None
            if tools is None
            else tuple(McpToolView(name=t.name, description=t.description) for t in tools),
            skipped=row.test.skipped,
            fingerprint=row.test.fingerprint,
        )
    return McpServerView(name=row.name, url=row.url, status=row.status, added_at=row.added_at, test=test)


class CustomMcp(Protocol):
    """What the MCP routes need (:class:`DashboardCustomMcp` in real use)."""

    async def servers(self) -> list[CustomMcpRow]:
        """Every server added and not removed."""
        ...

    async def add(self, name: str, url: str) -> tuple[McpVerdict, str | None]:
        """Add a server as a request."""
        ...

    async def test(self, name: str) -> McpVerdict:
        """List one server's tools and keep what was found."""
        ...

    async def remove(self, name: str) -> McpVerdict:
        """Remove one server."""
        ...


class DashboardCustomMcp:
    """The MCP tab's actions: the dashboard on this laptop acts as the Chairman (no login yet)."""

    def __init__(self, control: CustomMcpControl, clock: Callable[[], datetime]) -> None:
        self._control = control
        self._clock = clock

    async def servers(self) -> list[CustomMcpRow]:
        """Every server added and not removed."""
        return await self._control.servers()

    async def add(self, name: str, url: str) -> tuple[McpVerdict, str | None]:
        """Add a server as a request."""
        return await self._control.add(
            name, url, role=Role.CHAIRMAN, channel=Channel.DASHBOARD, now=self._clock()
        )

    async def test(self, name: str) -> McpVerdict:
        """List one server's tools and keep what was found."""
        return await self._control.test(
            name, role=Role.CHAIRMAN, channel=Channel.DASHBOARD, now=self._clock()
        )

    async def remove(self, name: str) -> McpVerdict:
        """Remove one server."""
        return await self._control.remove(
            name, role=Role.CHAIRMAN, channel=Channel.DASHBOARD, now=self._clock()
        )


def custom_mcp_routes(app: FastAPI, mcp: CustomMcp | None, refuse_foreign: Callable[..., Any]) -> None:
    """The MCP tab's routes. Without ``mcp`` (no database) the tab says servers can't be added here."""

    def ready() -> CustomMcp:
        if mcp is None:
            raise HTTPException(status_code=503, detail="mcp.not_available")
        return mcp

    async def page() -> McpPage:
        if mcp is None:
            return McpPage(available=False)
        try:
            return McpPage(available=True, servers=tuple(server_view(row) for row in await mcp.servers()))
        except StoreError as exc:
            say(get_logger("agentzaaheen.api"), "custom_mcp_store_failed", code=exc.code.value)
            raise HTTPException(status_code=503, detail="mcp.store_unavailable") from None

    def refuse(verdict: McpVerdict, why: str | None = None) -> None:
        if verdict is not McpVerdict.DONE:
            detail: dict[str, str | None] | str = (
                verdict.value if why is None else {"code": verdict.value, "why": why}
            )
            raise HTTPException(status_code=_REFUSED_STATUS[verdict], detail=detail)

    async def guarded[T](call: Callable[[], Awaitable[T]]) -> T:
        try:
            return await call()
        except StoreError as exc:
            say(get_logger("agentzaaheen.api"), "custom_mcp_store_failed", code=exc.code.value)
            raise HTTPException(status_code=503, detail="mcp.store_unavailable") from None

    @app.get("/api/mcp")
    async def get_mcp() -> McpPage:
        return await page()

    @app.post("/api/mcp", dependencies=[Depends(refuse_foreign)])
    async def add_mcp(body: McpAdd) -> McpPage:
        verdict, why = await guarded(lambda: ready().add(body.name, body.url))
        refuse(verdict, why)
        return await page()

    @app.post("/api/mcp/{name}/test", dependencies=[Depends(refuse_foreign)])
    async def test_mcp(name: str = Path(pattern=_NAME)) -> McpPage:
        refuse(await guarded(lambda: ready().test(name)))
        return await page()

    @app.post("/api/mcp/{name}/remove", dependencies=[Depends(refuse_foreign)])
    async def remove_mcp(name: str = Path(pattern=_NAME)) -> McpPage:
        refuse(await guarded(lambda: ready().remove(name)))
        return await page()

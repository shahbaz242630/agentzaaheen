"""Custom MCP servers through the policy layer (ADR-041 slice 1), against a real database.

Adding files a request that nothing can use; names of ours are refused; a
test keeps what it found (or why not); a removed server stays removed; the
app can never change a server's name or address; every attempt, refusals
included, is audited and the chain still verifies.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from agentzaaheen.core.net import NetError, NetErrorCode, check_mcp_url
from agentzaaheen.core.policy import Channel, CustomMcpControl, Ledger, McpVerdict, Role
from agentzaaheen.core.store import CustomMcpRow
from tests.integration.conftest import ScratchDb, connect_as, open_db, run

TENANT = uuid4()
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
ME = {"role": Role.CHAIRMAN, "channel": Channel.DASHBOARD, "now": NOW}
URL = "https://mcp.example.com/mcp"
PRINT = "c" * 64


@dataclass(frozen=True)
class Tool:
    name: str
    description: str


@dataclass(frozen=True)
class Found:
    server_name: str = "Example"
    tools: Sequence[Tool] = field(default_factory=lambda: (Tool("search", "Search the web."),))
    skipped: int = 1
    fingerprint: str = PRINT


class FakeTester:
    """Answers like ``net.list_tools``: a listing, or a NetError. Records each address it was asked."""

    def __init__(self, answer: Found | NetError | None = None) -> None:
        self.answer = answer or Found()
        self.asked: list[str] = []

    async def __call__(self, url: str) -> Found:
        self.asked.append(url)
        if isinstance(self.answer, NetError):
            raise self.answer
        return self.answer


def with_control[T](
    test_db: ScratchDb,
    use: Callable[[CustomMcpControl], Awaitable[T]],
    tester: FakeTester | None = None,
    taken: Sequence[str] = ("github", "triage-failing-build"),
) -> T:
    async def body() -> T:
        async with open_db(test_db.app_url) as db:
            control = CustomMcpControl(
                db, TENANT, taken=taken, check_address=check_mcp_url, tester=tester or FakeTester()
            )
            return await use(control)

    return run(body())


def audit(test_db: ScratchDb) -> list[tuple[str, str, str, str | None]]:
    async def body() -> list[tuple[str, str, str, str | None]]:
        async with open_db(test_db.app_url) as db:
            assert (await Ledger(db, TENANT).verify()).ok
        conn = await connect_as(test_db.app_url, TENANT)
        try:
            rows = await conn.fetch(
                "SELECT action, outcome, reason_code, target_ref FROM agentzaaheen.audit_log"
                " WHERE tenant_id = $1 ORDER BY seq",
                TENANT,
            )
        finally:
            await conn.close()
        return [(r["action"], r["outcome"], r["reason_code"], r["target_ref"]) for r in rows]

    return run(body())


def test_adding_files_a_request_with_a_clean_address_and_calls_nothing(test_db: ScratchDb) -> None:
    tester = FakeTester()

    async def use(control: CustomMcpControl) -> tuple[tuple[McpVerdict, str | None], list[CustomMcpRow]]:
        added = await control.add("web-search", " https://MCP.example.com/mcp ", **ME)  # type: ignore[arg-type]
        return added, await control.servers()

    added, servers = with_control(test_db, use, tester)
    assert added == (McpVerdict.DONE, None)
    (server,) = servers
    assert (server.name, server.url, server.status, server.test) == ("web-search", URL, "requested", None)
    assert (server.added_by, server.added_at) == ("user:chairman", NOW)
    assert tester.asked == []  # adding never contacts the server
    assert audit(test_db) == [("custom_mcp.add", "allow", "mcp.done", "custom_mcp:web-search")]


@pytest.mark.parametrize(
    ("name", "verdict"),
    [
        ("Web", McpVerdict.BAD_NAME),
        ("1web", McpVerdict.BAD_NAME),
        ("web search", McpVerdict.BAD_NAME),
        ("w" * 33, McpVerdict.BAD_NAME),
        ("", McpVerdict.BAD_NAME),
        ("github", McpVerdict.RESERVED_NAME),  # one of our connectors
        ("triage-failing-build", McpVerdict.RESERVED_NAME),  # one of our skills
        ("vault", McpVerdict.RESERVED_NAME),
        ("computer", McpVerdict.RESERVED_NAME),
        ("agentzaaheen", McpVerdict.RESERVED_NAME),
    ],
)
def test_names_that_are_odd_or_ours_are_refused(test_db: ScratchDb, name: str, verdict: McpVerdict) -> None:
    async def use(control: CustomMcpControl) -> tuple[tuple[McpVerdict, str | None], list[CustomMcpRow]]:
        return await control.add(name, URL, **ME), await control.servers()  # type: ignore[arg-type]

    added, servers = with_control(test_db, use)
    assert added == (verdict, None)
    assert servers == []
    ((action, outcome, reason, target),) = audit(test_db)
    assert (action, outcome, reason) == ("custom_mcp.add", "deny", verdict.value)
    assert target in {f"custom_mcp:{name}", "custom_mcp:(not a usable name)"}


def test_a_bad_address_is_refused_with_the_rule_in_plain_words(test_db: ScratchDb) -> None:
    async def use(control: CustomMcpControl) -> tuple[McpVerdict, str | None]:
        return await control.add("web", "https://user:pw@mcp.example.com/", **ME)  # type: ignore[arg-type]

    assert with_control(test_db, use) == (
        McpVerdict.BAD_ADDRESS,
        "an address may not carry a user or password",
    )


def test_one_live_server_per_name_and_the_name_is_free_again_after_removing(test_db: ScratchDb) -> None:
    async def use(control: CustomMcpControl) -> list[object]:
        return [
            (await control.add("web", URL, **ME))[0],  # type: ignore[arg-type]
            (await control.add("web", "https://other.example.com/", **ME))[0],  # type: ignore[arg-type]
            await control.remove("web", **ME),  # type: ignore[arg-type]
            await control.remove("web", **ME),  # type: ignore[arg-type]
            (await control.add("web", "https://other.example.com/", **ME))[0],  # type: ignore[arg-type]
            [s.url for s in await control.servers()],
        ]

    assert with_control(test_db, use) == [
        McpVerdict.DONE,
        McpVerdict.NAME_TAKEN,
        McpVerdict.DONE,
        McpVerdict.UNKNOWN_SERVER,
        McpVerdict.DONE,
        ["https://other.example.com/"],
    ]


def test_the_list_holds_fifty_live_servers_at_most(test_db: ScratchDb) -> None:
    async def use(control: CustomMcpControl) -> tuple[list[McpVerdict], McpVerdict, int]:
        added = [(await control.add(f"s{i}", URL, **ME))[0] for i in range(50)]  # type: ignore[arg-type]
        over = (await control.add("one-more", URL, **ME))[0]  # type: ignore[arg-type]
        return added, over, len(await control.servers())

    added, over, kept = with_control(test_db, use)
    assert set(added) == {McpVerdict.DONE}
    assert (over, kept) == (McpVerdict.NAME_TAKEN, 50)


def test_a_test_keeps_the_tools_it_listed(test_db: ScratchDb) -> None:
    tester = FakeTester()

    async def use(control: CustomMcpControl) -> tuple[McpVerdict, list[CustomMcpRow]]:
        await control.add("web", URL, **ME)  # type: ignore[arg-type]
        return await control.test("web", **ME), await control.servers()  # type: ignore[arg-type]

    verdict, (server,) = with_control(test_db, use, tester)
    assert verdict is McpVerdict.DONE
    assert tester.asked == [URL]  # the stored address, never anything the page sent
    assert server.test is not None
    assert (server.test.outcome, server.test.server_name, server.test.skipped) == ("mcp.listed", "Example", 1)
    assert [(t.name, t.description) for t in server.test.tools or ()] == [("search", "Search the web.")]
    assert server.test.fingerprint == PRINT
    assert audit(test_db)[-1] == ("custom_mcp.test", "allow", "mcp.done", "custom_mcp:web")


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (NetError(NetErrorCode.AUTH, "x", status=401), "mcp.needs_key"),
        (NetError(NetErrorCode.TIMEOUT, "x"), "mcp.no_answer"),
        (NetError(NetErrorCode.BLOCKED, "x"), "mcp.blocked"),
        (NetError(NetErrorCode.BAD_RESPONSE, "x"), "mcp.not_mcp"),
        (NetError(NetErrorCode.TOO_LARGE, "x"), "mcp.too_big"),
    ],
)
def test_a_failed_test_keeps_why_and_drops_the_old_tools(
    test_db: ScratchDb, error: NetError, outcome: str
) -> None:
    tester = FakeTester()

    async def use(control: CustomMcpControl) -> CustomMcpRow:
        await control.add("web", URL, **ME)  # type: ignore[arg-type]
        await control.test("web", **ME)  # type: ignore[arg-type]
        tester.answer = error
        later = {**ME, "now": NOW + timedelta(minutes=5)}
        assert await control.test("web", **later) is McpVerdict.DONE  # type: ignore[arg-type]
        (server,) = await control.servers()
        return server

    server = with_control(test_db, use, tester)
    assert server.test is not None
    assert (server.test.outcome, server.test.at) == (outcome, NOW + timedelta(minutes=5))
    assert (server.test.tools, server.test.fingerprint, server.test.server_name) == (None, None, None)


def test_unknown_servers_are_not_tested(test_db: ScratchDb) -> None:
    tester = FakeTester()

    async def use(control: CustomMcpControl) -> McpVerdict:
        return await control.test("nobody", **ME)  # type: ignore[arg-type]

    assert with_control(test_db, use, tester) is McpVerdict.UNKNOWN_SERVER
    assert tester.asked == []


@pytest.mark.parametrize(
    ("role", "channel"),
    [
        (Role.VIEWER, Channel.DASHBOARD),
        (Role.AI_OPERATOR, Channel.MCP),
        (Role.AGENT_AUDITOR, Channel.SYSTEM),
        (Role.CHAIRMAN, Channel.MCP),
    ],
)
def test_only_the_chairman_may_add_test_or_remove(test_db: ScratchDb, role: Role, channel: Channel) -> None:
    tester = FakeTester()
    them = {"role": role, "channel": channel, "now": NOW}

    async def use(control: CustomMcpControl) -> list[object]:
        await control.add("web", URL, **ME)  # type: ignore[arg-type]
        return [
            (await control.add("other", URL, **them))[0],  # type: ignore[arg-type]
            await control.test("web", **them),  # type: ignore[arg-type]
            await control.remove("web", **them),  # type: ignore[arg-type]
            [s.name for s in await control.servers()],
        ]

    assert with_control(test_db, use, tester) == [McpVerdict.NOT_ALLOWED] * 3 + [["web"]]
    assert tester.asked == []
    assert [row[2] for row in audit(test_db)][1:] == ["mcp.not_allowed"] * 3


def test_the_database_keeps_names_and_addresses_fixed_and_removed_rows_removed(test_db: ScratchDb) -> None:
    async def use(control: CustomMcpControl) -> None:
        await control.add("web", URL, **ME)  # type: ignore[arg-type]
        await control.add("gone", URL, **ME)  # type: ignore[arg-type]
        await control.remove("gone", **ME)  # type: ignore[arg-type]

    with_control(test_db, use)

    async def tamper() -> list[str]:
        refused: list[str] = []
        conn = await connect_as(test_db.app_url, TENANT)
        try:
            add = (
                "INSERT INTO agentzaaheen.custom_mcp_servers"
                " (id, tenant_id, name, url, status, added_by, added_at)"
                " VALUES ($1, $2, $3, $4, 'requested', 'user:x', now())"
            )
            for sql, args in (
                (
                    "UPDATE agentzaaheen.custom_mcp_servers SET url = 'https://evil.example.com/'"
                    " WHERE name = 'web'",
                    (),
                ),
                ("UPDATE agentzaaheen.custom_mcp_servers SET name = 'webx' WHERE name = 'web'", ()),
                ("DELETE FROM agentzaaheen.custom_mcp_servers WHERE name = 'web'", ()),
                (
                    "UPDATE agentzaaheen.custom_mcp_servers SET status = 'requested', removed_by = NULL,"
                    " removed_at = NULL WHERE name = 'gone'",
                    (),
                ),
                ("UPDATE agentzaaheen.custom_mcp_servers SET status = 'approved' WHERE name = 'web'", ()),
                (
                    "UPDATE agentzaaheen.custom_mcp_servers SET tools = '[]'::jsonb WHERE name = 'web'",
                    (),
                ),
                (add, (uuid4(), uuid4(), "x", "https://a.example.com/")),  # another tenant
                (add, (uuid4(), TENANT, "y", "http://a.example.com/")),  # plain http
            ):
                try:
                    await conn.execute(sql, *args)
                except asyncpg.PostgresError as exc:
                    refused.append(type(exc).__name__)
                else:
                    refused.append("ALLOWED")
        finally:
            await conn.close()
        return refused

    assert "ALLOWED" not in run(tamper())


def test_another_tenant_sees_none_of_them(test_db: ScratchDb) -> None:
    async def use(control: CustomMcpControl) -> None:
        await control.add("web", URL, **ME)  # type: ignore[arg-type]

    with_control(test_db, use)

    async def look() -> list[CustomMcpRow]:
        async with open_db(test_db.app_url) as db:
            other = CustomMcpControl(db, uuid4(), taken=(), check_address=check_mcp_url, tester=FakeTester())
            return await other.servers()

    assert run(look()) == []

    async def raw() -> list[str]:  # past our own code: the database wall alone must hold
        conn = await connect_as(test_db.app_url, uuid4())
        try:
            return [r["name"] for r in await conn.fetch("SELECT name FROM agentzaaheen.custom_mcp_servers")]
        finally:
            await conn.close()

    assert run(raw()) == []

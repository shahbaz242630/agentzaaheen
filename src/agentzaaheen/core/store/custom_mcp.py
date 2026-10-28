"""Custom MCP server rows: added, tested, removed (migration 0019, ADR-041 slice 1).

The store keeps what it is told under the database's rules: one live row per
name; the name and address never change; a removed row never changes again.
Whether a name or address is allowed, and who may add, test or remove, is
the policy layer's job. There is no approval here: nothing in these rows can
be used by an agent or the chat.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from agentzaaheen.core.store.db import Scoped, require_aware, written

MAX_SERVERS = 50  # per tenant, live ones; the page lists them all

_LIVE = """
SELECT id, name, url, status, added_by, added_at, tested_at, test_outcome, server_name, tools, tools_skipped,
    tools_fingerprint
FROM agentzaaheen.custom_mcp_servers
WHERE tenant_id = $1 AND status <> 'removed' ORDER BY added_at, id LIMIT 50
"""
_GET = """
SELECT id, name, url, status, added_by, added_at, tested_at, test_outcome, server_name, tools, tools_skipped,
    tools_fingerprint
FROM agentzaaheen.custom_mcp_servers
WHERE tenant_id = $1 AND name = $2 AND status <> 'removed'
"""
_COUNT = "SELECT count(*) FROM agentzaaheen.custom_mcp_servers WHERE tenant_id = $1 AND status <> 'removed'"
_ADD = """
INSERT INTO agentzaaheen.custom_mcp_servers (id, tenant_id, name, url, status, added_by, added_at)
VALUES ($1, $2, $3, $4, 'requested', $5, $6)
ON CONFLICT (tenant_id, name) WHERE status <> 'removed' DO NOTHING
RETURNING id
"""
_TESTED = """
UPDATE agentzaaheen.custom_mcp_servers SET tested_at = $3, test_outcome = $4, server_name = $5, tools = $6,
    tools_skipped = $7, tools_fingerprint = $8
WHERE tenant_id = $1 AND id = $2 AND status <> 'removed'
RETURNING id
"""
_REMOVE = """
UPDATE agentzaaheen.custom_mcp_servers SET status = 'removed', removed_by = $3, removed_at = $4
WHERE tenant_id = $1 AND name = $2 AND status <> 'removed'
RETURNING id
"""


@dataclass(frozen=True)
class McpToolRow:
    """One listed tool, in the server's own (untrusted) words."""

    name: str
    description: str


@dataclass(frozen=True)
class McpTest:
    """What one test found. ``outcome`` is a code (``mcp.listed`` when tools came back)."""

    at: datetime
    outcome: str
    server_name: str | None = None
    tools: tuple[McpToolRow, ...] | None = None
    skipped: int | None = None
    fingerprint: str | None = None


@dataclass(frozen=True)
class CustomMcpRow:
    """One custom MCP server that is not removed."""

    id: UUID
    name: str
    url: str
    status: str
    added_by: str
    added_at: datetime
    test: McpTest | None


def _row(record: asyncpg.Record) -> CustomMcpRow:
    test = None
    if record["tested_at"] is not None:
        raw: list[dict[str, Any]] | None = record["tools"]
        test = McpTest(
            at=record["tested_at"],
            outcome=record["test_outcome"],
            server_name=record["server_name"],
            tools=None
            if raw is None
            else tuple(McpToolRow(name=t["name"], description=t["description"]) for t in raw),
            skipped=record["tools_skipped"],
            fingerprint=record["tools_fingerprint"],
        )
    return CustomMcpRow(
        id=record["id"],
        name=record["name"],
        url=record["url"],
        status=record["status"],
        added_by=record["added_by"],
        added_at=record["added_at"],
        test=test,
    )


class CustomMcpRows:
    """Repository for one tenant's custom MCP servers."""

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope
        self._tenant = scope.tenant_id

    async def live(self) -> list[CustomMcpRow]:
        """Every server not removed, oldest first."""
        async with self._scope.connection() as conn:
            records = await conn.fetch(_LIVE, self._tenant)
        return [_row(r) for r in records]

    async def get(self, name: str) -> CustomMcpRow | None:
        """The live server called ``name``, or None."""
        async with self._scope.connection() as conn:
            record = await conn.fetchrow(_GET, self._tenant, name)
        return None if record is None else _row(record)

    async def add(  # each is a separate column
        self, server_id: UUID, name: str, url: str, *, added_by: str, added_at: datetime
    ) -> bool:
        """Add a server as a request. False if a live one already has that name, or the list is full."""
        require_aware(added_at)
        async with self._scope.write("a custom MCP server added") as conn:
            if await conn.fetchval(_COUNT, self._tenant) >= MAX_SERVERS:
                return False
            return written(await conn.fetchval(_ADD, server_id, self._tenant, name, url, added_by, added_at))

    async def record_test(self, server_id: UUID, test: McpTest) -> bool:
        """Keep what a test found (replacing the last). False if the server was removed meanwhile."""
        require_aware(test.at)
        tools = (
            None
            if test.tools is None
            else [{"name": t.name, "description": t.description} for t in test.tools]
        )
        async with self._scope.write("a custom MCP server's test result") as conn:
            return written(
                await conn.fetchval(
                    _TESTED,
                    self._tenant,
                    server_id,
                    test.at,
                    test.outcome,
                    test.server_name,
                    tools,
                    test.skipped,
                    test.fingerprint,
                )
            )

    async def remove(self, name: str, *, by: str, at: datetime) -> bool:
        """Mark the live server ``name`` removed (the row is kept). False if there is none."""
        require_aware(at)
        async with self._scope.write("a custom MCP server removed") as conn:
            return written(await conn.fetchval(_REMOVE, self._tenant, name, by, at))

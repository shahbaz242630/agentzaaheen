"""Custom MCP servers: add as a request, test by listing tools, remove (ADR-041 slice 1).

Every attempt is judged by :func:`authorize` (who, from where), then by the
rules below, and written to the audit log, refusals included.

- **Names** are lowercase letters, digits, ``-`` or ``_``, start with a
  letter, at most 32 characters, and may not be one of ours (a registry
  item's id or a reserved word), so a custom server can never pose as
  something Agent Zaaheen already has.
- **Addresses** are checked by the network layer's rule (public https only,
  no user, password, query or fragment) before anything is kept.
- **A test** only lists tools (``net.list_tools``); what it found replaces
  the last test. It sends none of our keys or text.

Nothing here can switch a server on or give it to an agent: approval with
the passkey is slice 2. The policy layer never talks to the network itself:
the address check and the test are handed in.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from agentzaaheen.core.policy.access import Channel, Operation, Role, authorize
from agentzaaheen.core.policy.audit import AuditDraft, AuditOutcome
from agentzaaheen.core.policy.ledger import Ledger
from agentzaaheen.core.sdk import SourceError, get_logger, say
from agentzaaheen.core.store import CustomMcpRow, CustomMcpRows, Database, McpTest, McpToolRow

NAME_RULE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# Words of ours a custom server may never take, besides every registry item's id.
RESERVED_NAMES = frozenset(
    {
        "admin", "agent", "agents", "agentzaaheen", "anthropic", "board", "broker", "browser", "chat",
        "claude", "cloudflare", "computer", "dashboard", "files", "github", "grafana", "healthchecks", "mcp",
        "ntfy", "paddle", "policy", "registry", "runner", "shell", "supabase", "system", "uptime", "vault",
        "zaaheen",
    }
)  # fmt: skip
_ACTORS: Mapping[Role, str] = {
    Role.CHAIRMAN: "user:chairman",
    Role.VIEWER: "user:viewer",
    Role.AI_OPERATOR: "operator:claude",
    Role.AGENT_AUDITOR: "system:agent-auditor",
}
# What a failed test says, from the network layer's reason code.
_OUTCOMES: Mapping[str, str] = {
    "net.blocked": "mcp.blocked",
    "net.auth": "mcp.needs_key",
    "net.timeout": "mcp.no_answer",
    "net.unavailable": "mcp.no_answer",
    "net.rate_limited": "mcp.busy",
    "net.server_error": "mcp.server_error",
    "net.client_error": "mcp.refused",
    "net.too_large": "mcp.too_big",
    "net.bad_response": "mcp.not_mcp",
}
LISTED = "mcp.listed"
FAILED = "mcp.failed"  # any failure the network layer did not name


class McpVerdict(StrEnum):
    """What happened to one attempt. Safe to log, audit and show."""

    DONE = "mcp.done"
    NOT_ALLOWED = "mcp.not_allowed"
    BAD_NAME = "mcp.bad_name"
    RESERVED_NAME = "mcp.reserved_name"
    BAD_ADDRESS = "mcp.bad_address"
    NAME_TAKEN = "mcp.name_taken"  # a live server has that name, or the list is full
    UNKNOWN_SERVER = "mcp.unknown_server"


class ToolSeen(Protocol):
    """One tool a test listed."""

    @property
    def name(self) -> str:
        """The tool's name."""
        ...

    @property
    def description(self) -> str:
        """What the server says the tool does (untrusted)."""
        ...


class Listing(Protocol):
    """What a test found (``net.McpListing``)."""

    @property
    def server_name(self) -> str:
        """The name the server gave itself (untrusted)."""
        ...

    @property
    def tools(self) -> Sequence[ToolSeen]:
        """Every usable tool listed."""
        ...

    @property
    def skipped(self) -> int:
        """Tools left out because their name was not usable."""
        ...

    @property
    def fingerprint(self) -> str:
        """SHA-256 over every tool's name, description and input shape."""
        ...


AddressCheck = Callable[[str], str]  # the cleaned address, or raises SourceError
Tester = Callable[[str], Awaitable[Listing]]


class CustomMcpControl:
    """One tenant's custom MCP servers: list, add, test, remove."""

    def __init__(  # the store, then what this tenant already has, then the two network steps
        self,
        db: Database,
        tenant_id: UUID,
        *,
        taken: Iterable[str],
        check_address: AddressCheck,
        tester: Tester,
    ) -> None:
        self._rows = CustomMcpRows(db.scoped(tenant_id))
        self._ledger = Ledger(db, tenant_id)
        self._reserved = RESERVED_NAMES | frozenset(taken)
        self._check_address = check_address
        self._tester = tester
        self._log = get_logger("agentzaaheen.custom_mcp")

    async def servers(self) -> list[CustomMcpRow]:
        """Every server added and not removed, with its last test."""
        return await self._rows.live()

    async def add(  # the server, then who asked from where
        self, name: str, url: str, *, role: Role, channel: Channel, now: datetime
    ) -> tuple[McpVerdict, str | None]:
        """Add a server as a request (nothing calls it). With BAD_ADDRESS, also why, in plain words."""
        why: str | None = None
        address = ""
        verdict = self._judge(Operation.ADD_CUSTOM_MCP, role, channel, now) or self._name_problem(name)
        if verdict is None:
            try:
                address = self._check_address(url)
            except SourceError as exc:
                verdict, why = McpVerdict.BAD_ADDRESS, getattr(exc, "plain", None)
        if verdict is None:
            added = await self._rows.add(uuid4(), name, address, added_by=_ACTORS[role], added_at=now)
            verdict = McpVerdict.DONE if added else McpVerdict.NAME_TAKEN
        await self._audit("custom_mcp.add", verdict, role, now=now, name=name)
        return verdict, why

    async def test(self, name: str, *, role: Role, channel: Channel, now: datetime) -> McpVerdict:
        """List the server's tools and keep what was found. DONE means the test ran, whatever it found."""
        outcome: str | None = None
        verdict = self._judge(Operation.TEST_CUSTOM_MCP, role, channel, now)
        if verdict is None:
            server = await self._rows.get(name)
            if server is None:
                verdict = McpVerdict.UNKNOWN_SERVER
            else:
                found = await self._run(server.url, now)
                outcome = found.outcome
                kept = await self._rows.record_test(server.id, found)  # removed meanwhile: not kept
                verdict = McpVerdict.DONE if kept else McpVerdict.UNKNOWN_SERVER
        await self._audit("custom_mcp.test", verdict, role, now=now, name=name, outcome=outcome)
        return verdict

    async def remove(self, name: str, *, role: Role, channel: Channel, now: datetime) -> McpVerdict:
        """Remove a server (its row is kept as a record)."""
        verdict = self._judge(Operation.REMOVE_CUSTOM_MCP, role, channel, now)
        if verdict is None:
            removed = await self._rows.remove(name, by=_ACTORS[role], at=now)
            verdict = McpVerdict.DONE if removed else McpVerdict.UNKNOWN_SERVER
        await self._audit("custom_mcp.remove", verdict, role, now=now, name=name)
        return verdict

    async def _run(self, url: str, now: datetime) -> McpTest:
        try:
            listing = await self._tester(url)
        except SourceError as exc:
            return McpTest(at=now, outcome=_OUTCOMES.get(exc.reason_code, FAILED))
        return McpTest(
            at=now,
            outcome=LISTED,
            server_name=listing.server_name or None,
            tools=tuple(McpToolRow(name=t.name, description=t.description) for t in listing.tools),
            skipped=listing.skipped,
            fingerprint=listing.fingerprint,
        )

    @staticmethod
    def _judge(operation: Operation, role: Role, channel: Channel, now: datetime) -> McpVerdict | None:
        if not authorize(role, operation, channel, step_up_at=None, now=now).allowed:
            return McpVerdict.NOT_ALLOWED
        return None

    def _name_problem(self, name: str) -> McpVerdict | None:
        if not NAME_RULE.match(name):
            return McpVerdict.BAD_NAME
        if name in self._reserved:
            return McpVerdict.RESERVED_NAME
        return None

    async def _audit(  # noqa: PLR0913  # each is a separate audit field
        self,
        action: str,
        verdict: McpVerdict,
        role: Role,
        *,
        now: datetime,
        name: str,
        outcome: str | None = None,
    ) -> None:
        shown = name if NAME_RULE.match(name) else "(not a usable name)"
        say(
            self._log,
            "custom_mcp_decided",
            action=action,
            actor=_ACTORS[role],
            server=shown,
            verdict=verdict.value,
            outcome=outcome,
        )
        details: dict[str, str | int | bool | None] = {"verdict": verdict.value}
        if outcome is not None:
            details["outcome"] = outcome
        await self._ledger.record(
            AuditDraft(
                at=now,
                actor=_ACTORS[role],
                action=action,
                outcome=AuditOutcome.ALLOW if verdict is McpVerdict.DONE else AuditOutcome.DENY,
                reason_code=verdict.value,
                correlation_id=uuid4(),
                target_ref=f"custom_mcp:{shown}",
                details=details,
            )
        )

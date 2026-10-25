"""Where policy state meets the database: the stored audit chain and the kill switches.

:class:`Ledger` computes each audit entry's hash here (policy owns the chain
rules) and asks the store to insert it under the tenant's lock, so two writers
can never fork the chain. :meth:`Ledger.verify` reloads every row and checks it.

:func:`load_kill_switches` fails closed: if the switches cannot be read, or a
stored switch makes no sense, the answer is "stop everything".
"""

from __future__ import annotations

from typing import TextIO
from uuid import UUID

from pydantic import ValidationError

from agentzaaheen.core.policy.audit import (
    GENESIS_HASH,
    AuditDraft,
    AuditEntry,
    ChainAnchor,
    ChainReport,
    append,
    verify_chain,
)
from agentzaaheen.core.policy.gate import KillSwitches
from agentzaaheen.core.sdk import get_logger, say
from agentzaaheen.core.store import AuditRow, AuditRows, Database, KillSwitchRows, StoreError, SwitchScope

_STOP_EVERYTHING = KillSwitches(stop_all=True)


def entry_from_row(row: AuditRow) -> AuditEntry:
    """Rebuild a policy audit entry from a stored row (validated again on the way in)."""
    fields = {name: getattr(row, name) for name in AuditEntry.model_fields}
    return AuditEntry.model_validate(fields)


def row_from_entry(tenant_id: UUID, entry: AuditEntry) -> AuditRow:
    """The row the store keeps for one entry."""
    return AuditRow(
        tenant_id=tenant_id,
        seq=entry.seq,
        at=entry.at,
        actor=entry.actor,
        action=entry.action,
        outcome=entry.outcome.value,
        reason_code=entry.reason_code,
        correlation_id=entry.correlation_id,
        project_id=entry.project_id,
        target_ref=entry.target_ref,
        details=dict(entry.details),
        prev_hash=entry.prev_hash,
        hash=entry.hash,
    )


class Ledger:
    """One tenant's audit chain, stored in Postgres."""

    def __init__(self, db: Database, tenant_id: UUID) -> None:
        self._rows = AuditRows(db.scoped(tenant_id))
        self._tenant_id = tenant_id

    async def record(self, draft: AuditDraft) -> AuditEntry:
        """Append one decision to the chain and return the stored entry."""

        def build(head: AuditRow | None) -> AuditRow:
            previous = None if head is None else entry_from_row(head)
            return row_from_entry(self._tenant_id, append(previous, draft))

        return entry_from_row(await self._rows.append(build))

    async def verify(self, anchor: ChainAnchor | None = None) -> ChainReport:
        """Reload the whole chain and check every link (and that it still contains ``anchor``)."""
        entries: list[AuditEntry] = []
        for row in await self._rows.all_rows():
            try:
                entries.append(entry_from_row(row))
            except ValidationError:
                head = entries[-1] if entries else None
                return ChainReport(
                    ok=False,
                    checked=len(entries),
                    head=ChainAnchor(seq=head.seq, hash=head.hash)
                    if head
                    else ChainAnchor(seq=0, hash=GENESIS_HASH),
                    problem="stored row is not a valid audit entry",
                    bad_seq=row.seq,
                )
        return verify_chain(entries, anchor=anchor)


def _switches_from(engaged: list[tuple[SwitchScope, str]]) -> KillSwitches:
    projects = frozenset(UUID(ref) for scope, ref in engaged if scope is SwitchScope.PROJECT)
    agents = frozenset(ref for scope, ref in engaged if scope is SwitchScope.AGENT)
    stop_all = any(scope is SwitchScope.ALL for scope, _ in engaged)
    return KillSwitches(stop_all=stop_all, paused_agents=agents, frozen_projects=projects)


async def load_kill_switches(
    db: Database, tenant_id: UUID, *, log_sink: TextIO | None = None
) -> KillSwitches:
    """Current kill switches for a tenant. Any failure means stop everything (SP-2)."""
    log = get_logger("policy.ledger", sink=log_sink, tenant_id=str(tenant_id))
    try:
        engaged = await KillSwitchRows(db.scoped(tenant_id)).engaged()
        return _switches_from([(s.scope, s.ref) for s in engaged])
    except StoreError as exc:
        say(log, "kill_switches_unreadable", reason_code=exc.code.value, effect="stop_all")
        return _STOP_EVERYTHING
    except ValueError:  # a stored scope or ref that makes no sense
        say(log, "kill_switch_row_invalid", reason_code="policy.bad_switch_row", effect="stop_all")
        return _STOP_EVERYTHING

"""Switching registry items off, and asking for them to be switched on (ADR-030, F06 slice 2b).

Every attempt is judged by :func:`authorize` (who, from where), then by the
item's state (see ``registry.switches``), and written to the audit log, refusals
included. Off happens at once. "On" is only ever *asked for* here: the request
waits for the Chairman's approval with a passkey (slice 2c), so nothing in
this module can widen what a model may use.

The registry is the one read at start; an item's fingerprint is that of its
files then. A request is tied to that fingerprint; one whose item has changed
since is shown as stale and can no longer be approved.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from yaqzan.core.policy.access import Channel, Operation, Role, authorize
from yaqzan.core.policy.audit import AuditDraft, AuditOutcome
from yaqzan.core.policy.ledger import Ledger
from yaqzan.core.registry import (
    Kind,
    Registry,
    State,
    StoredSwitch,
    SwitchKey,
    by_item,
    may_request,
    may_switch_off,
    state_of,
)
from yaqzan.core.store import Database, RegistrySwitchRows, RequestRow, RequestStatus

# Plugin bundles come from outside folders (e.g. Claude's plugin cache): listed, never used (ADR-027).
OUTSIDE_KINDS = frozenset({Kind.PLUGIN})
_ACTORS: Mapping[Role, str] = {
    Role.CHAIRMAN: "user:chairman",
    Role.VIEWER: "user:viewer",
    Role.AI_OPERATOR: "operator:claude",
    Role.AGENT_AUDITOR: "system:agent-auditor",
}


class Verdict(StrEnum):
    """What happened to one attempt. Safe to log, audit and show."""

    DONE = "registry.done"
    NOT_ALLOWED = "registry.not_allowed"
    UNKNOWN_ITEM = "registry.unknown_item"
    VIEW_ONLY = "registry.view_only"
    NOT_ON = "registry.not_on"
    NOT_OFF = "registry.not_off"
    ALREADY_ASKED = "registry.already_asked"
    UNKNOWN_REQUEST = "registry.unknown_request"


@dataclass(frozen=True)
class Pending:
    """A request waiting for approval. ``stale``: the item's files changed since it was asked for."""

    id: UUID
    kind: Kind
    item_id: str
    requested_at: datetime
    stale: bool


@dataclass(frozen=True)
class Standing:
    """How every item stands now, and what is waiting for approval."""

    states: Mapping[SwitchKey, State]
    pending: tuple[Pending, ...]

    def on(self, kind: Kind) -> frozenset[str]:
        """The ids of every item of ``kind`` that is on."""
        return frozenset(i for (k, i), s in self.states.items() if k is kind and s is State.ON)


def _items(registry: Registry) -> dict[SwitchKey, str]:
    """Every item's fingerprint, keyed by (kind, id)."""
    items: dict[SwitchKey, str] = {}
    for kind, group in (
        (Kind.CONNECTOR, registry.connectors),
        (Kind.SKILL, registry.skills),
        (Kind.PLUGIN, registry.plugins),
    ):
        items.update({(kind, item.id): item.fingerprint for item in group})
    return items


class RegistryControl:
    """One tenant's registry switches: read how things stand, switch off, ask, withdraw."""

    def __init__(self, db: Database, tenant_id: UUID, registry: Registry) -> None:
        self._rows = RegistrySwitchRows(db)
        self._ledger = Ledger(db, tenant_id)
        self._tenant = tenant_id
        self._items = _items(registry)

    async def standing(self) -> Standing:
        """Every item's state and every pending request, from the database now."""
        stored = by_item(
            StoredSwitch(kind=Kind(r.kind), item_id=r.item_id, enabled=r.enabled, fingerprint=r.fingerprint)
            for r in await self._rows.switches(self._tenant)
        )
        states = {key: self._state(key, stored) for key in self._items}
        pending = tuple(
            Pending(
                id=r.id,
                kind=Kind(r.kind),
                item_id=r.item_id,
                requested_at=r.requested_at,
                stale=self._items.get((Kind(r.kind), r.item_id)) != r.fingerprint,
            )
            for r in await self._rows.pending(self._tenant)
        )
        return Standing(states=states, pending=pending)

    def _state(self, key: SwitchKey, stored: Mapping[SwitchKey, StoredSwitch]) -> State:
        kind, item_id = key
        return state_of(kind, item_id, self._items[key], stored, outside=kind in OUTSIDE_KINDS)

    async def switch_off(
        self, kind: Kind, item_id: str, *, role: Role, channel: Channel, now: datetime
    ) -> Verdict:
        """Switch one item off at once (it only takes power away)."""
        target = f"{kind.value}:{item_id}"
        verdict = await self._check(Operation.SWITCH_OFF, kind, item_id, role=role, channel=channel, now=now)
        if verdict is None:
            state = (await self.standing()).states[(kind, item_id)]
            verdict = Verdict.DONE if may_switch_off(state) else self._wrong_state(state, Verdict.NOT_ON)
        if verdict is Verdict.DONE:
            fingerprint = self._items[(kind, item_id)]
            await self._rows.switch_off(
                self._tenant, kind.value, item_id, fingerprint, changed_by=_ACTORS[role], changed_at=now
            )
        await self._audit("registry.switch_off", verdict, role, now=now, target=target)
        return verdict

    async def request_on(
        self, kind: Kind, item_id: str, *, role: Role, channel: Channel, now: datetime
    ) -> Verdict:
        """File a request to switch one item on. Nothing is switched on here."""
        target = f"{kind.value}:{item_id}"
        verdict = await self._check(Operation.REQUEST_ON, kind, item_id, role=role, channel=channel, now=now)
        if verdict is None:
            state = (await self.standing()).states[(kind, item_id)]
            verdict = Verdict.DONE if may_request(state) else self._wrong_state(state, Verdict.NOT_OFF)
        request_id: UUID | None = None
        if verdict is Verdict.DONE:
            request_id = uuid4()
            filed = RequestRow(
                id=request_id,
                kind=kind.value,
                item_id=item_id,
                fingerprint=self._items[(kind, item_id)],
                status=RequestStatus.PENDING,
                requested_by=_ACTORS[role],
                requested_at=now,
            )
            if not await self._rows.add_request(self._tenant, filed):
                verdict, request_id = Verdict.ALREADY_ASKED, None
        await self._audit("registry.request_on", verdict, role, now=now, target=target, request_id=request_id)
        return verdict

    async def withdraw(self, request_id: UUID, *, role: Role, channel: Channel, now: datetime) -> Verdict:
        """Withdraw a pending request."""
        verdict = Verdict.DONE
        if not authorize(role, Operation.WITHDRAW_REQUEST, channel, step_up_at=None, now=now).allowed:
            verdict = Verdict.NOT_ALLOWED
        elif not await self._rows.withdraw(self._tenant, request_id, by=_ACTORS[role], at=now):
            verdict = Verdict.UNKNOWN_REQUEST
        await self._audit(
            "registry.withdraw", verdict, role, now=now, target=f"request:{request_id}", request_id=request_id
        )
        return verdict

    async def _check(  # noqa: PLR0913  # the attempt, and who made it from where
        self, operation: Operation, kind: Kind, item_id: str, *, role: Role, channel: Channel, now: datetime
    ) -> Verdict | None:
        """A refusal that does not depend on the stored state, or ``None`` to go on."""
        if not authorize(role, operation, channel, step_up_at=None, now=now).allowed:
            return Verdict.NOT_ALLOWED
        if (kind, item_id) not in self._items:
            return Verdict.UNKNOWN_ITEM
        if kind in OUTSIDE_KINDS:
            return Verdict.VIEW_ONLY
        return None

    @staticmethod
    def _wrong_state(state: State, otherwise: Verdict) -> Verdict:
        return Verdict.VIEW_ONLY if state is State.VIEW_ONLY else otherwise

    async def _audit(  # noqa: PLR0913  # each is a separate audit field
        self,
        action: str,
        verdict: Verdict,
        role: Role,
        *,
        now: datetime,
        target: str,
        request_id: UUID | None = None,
    ) -> None:
        details: dict[str, str | int | bool | None] = {"verdict": verdict.value}
        if request_id is not None:
            details["request"] = str(request_id)
        await self._ledger.record(
            AuditDraft(
                at=now,
                actor=_ACTORS[role],
                action=action,
                outcome=AuditOutcome.ALLOW if verdict is Verdict.DONE else AuditOutcome.DENY,
                reason_code=verdict.value,
                correlation_id=uuid4(),
                target_ref=target,
                details=details,
            )
        )

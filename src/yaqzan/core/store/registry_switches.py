"""Registry switch rows and the requests to switch an item on (ADR-030).

The store only keeps what it is told, under the database's own rules: "on"
must point at an approved request for the same item and files, and a decided
request never changes. Deciding *whether* something counts as on (defaults,
changed files) is the registry's job; who may ask, and whether a passkey
was checked, is the policy layer's (ADR-SEC-007). The only way to switch an
item on is :meth:`RegistrySwitchRows.approve`, which needs a step-up time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

import asyncpg

from yaqzan.core.store.db import Scoped, require_aware, written

ItemKind = Literal["connector", "skill", "plugin"]

_SWITCHES = """
SELECT kind, item_id, enabled, fingerprint, request_id, changed_by, changed_at
FROM yaqzan.registry_switches WHERE tenant_id = $1 ORDER BY kind, item_id
"""
_SWITCH_OFF = """
INSERT INTO yaqzan.registry_switches (tenant_id, kind, item_id, enabled, fingerprint, request_id, changed_by,
    changed_at)
VALUES ($1, $2, $3, false, $4, NULL, $5, $6)
ON CONFLICT (tenant_id, kind, item_id) DO UPDATE SET
    enabled = false, fingerprint = EXCLUDED.fingerprint, request_id = NULL,
    changed_by = EXCLUDED.changed_by, changed_at = EXCLUDED.changed_at
"""
_PENDING = """
SELECT id, kind, item_id, fingerprint, status, requested_by, requested_at, decided_by, decided_at
FROM yaqzan.registry_requests WHERE tenant_id = $1 AND status = 'pending' ORDER BY requested_at, id
"""
_GET_REQUEST = """
SELECT id, kind, item_id, fingerprint, status, requested_by, requested_at, decided_by, decided_at
FROM yaqzan.registry_requests WHERE tenant_id = $1 AND id = $2
"""
_ADD_REQUEST = """
INSERT INTO yaqzan.registry_requests (id, tenant_id, kind, item_id, fingerprint, status, requested_by,
    requested_at)
VALUES ($1, $2, $3, $4, $5, 'pending', $6, $7)
ON CONFLICT (tenant_id, kind, item_id) WHERE status = 'pending' DO NOTHING
RETURNING id
"""
_APPROVE = """
UPDATE yaqzan.registry_requests SET status = 'approved', decided_by = $3, decided_at = $4, step_up_at = $5
WHERE tenant_id = $1 AND id = $2 AND status = 'pending'
RETURNING kind, item_id, fingerprint
"""
_SWITCH_ON = """
INSERT INTO yaqzan.registry_switches (tenant_id, kind, item_id, enabled, fingerprint, request_id, changed_by,
    changed_at)
VALUES ($1, $2, $3, true, $4, $5, $6, $7)
ON CONFLICT (tenant_id, kind, item_id) DO UPDATE SET
    enabled = true, fingerprint = EXCLUDED.fingerprint, request_id = EXCLUDED.request_id,
    changed_by = EXCLUDED.changed_by, changed_at = EXCLUDED.changed_at
"""
_REFUSE = """
UPDATE yaqzan.registry_requests SET status = 'refused', decided_by = $3, decided_at = $4
WHERE tenant_id = $1 AND id = $2 AND status = 'pending'
RETURNING id
"""
_WITHDRAW = """
UPDATE yaqzan.registry_requests SET status = 'withdrawn', decided_by = $3, decided_at = $4
WHERE tenant_id = $1 AND id = $2 AND status = 'pending'
RETURNING id
"""


class RequestStatus(StrEnum):
    """Where a request stands. Only ``pending`` can still change."""

    PENDING = "pending"
    APPROVED = "approved"
    REFUSED = "refused"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class SwitchRow:
    """One item's stored switch. ``request_id`` names the approval when ``enabled``."""

    kind: ItemKind
    item_id: str
    enabled: bool
    fingerprint: str
    request_id: UUID | None
    changed_by: str
    changed_at: datetime


@dataclass(frozen=True)
class RequestRow:
    """One request to switch an item on, for exactly the files with ``fingerprint``."""

    id: UUID
    kind: ItemKind
    item_id: str
    fingerprint: str
    status: RequestStatus
    requested_by: str
    requested_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None


def _request(record: asyncpg.Record) -> RequestRow:
    return RequestRow(**{**dict(record), "status": RequestStatus(record["status"])})


class RegistrySwitchRows:
    """Repository for registry switches and requests."""

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope
        self._tenant = scope.tenant_id

    async def switches(self) -> list[SwitchRow]:
        """Every stored switch for this tenant (items with no row keep their default)."""
        async with self._scope.connection() as conn:
            records = await conn.fetch(_SWITCHES, self._tenant)
        return [SwitchRow(**dict(r)) for r in records]

    async def switch_off(  # each argument is a separate column
        self,
        kind: ItemKind,
        item_id: str,
        fingerprint: str,
        *,
        changed_by: str,
        changed_at: datetime,
    ) -> None:
        """Store "off" for one item (always allowed: it only takes power away)."""
        require_aware(changed_at)
        async with self._scope.write("a Customize switch turned off") as conn:
            await conn.execute(_SWITCH_OFF, self._tenant, kind, item_id, fingerprint, changed_by, changed_at)

    async def pending(self) -> list[RequestRow]:
        """Every request still waiting for a decision, oldest first."""
        async with self._scope.connection() as conn:
            records = await conn.fetch(_PENDING, self._tenant)
        return [_request(r) for r in records]

    async def get_request(self, request_id: UUID) -> RequestRow | None:
        """One request, or None if it does not exist for this tenant."""
        async with self._scope.connection() as conn:
            record = await conn.fetchrow(_GET_REQUEST, self._tenant, request_id)
        return None if record is None else _request(record)

    async def add_request(self, request: RequestRow) -> bool:
        """File a pending request. Returns False if the item already has one waiting."""
        if request.status is not RequestStatus.PENDING:
            raise ValueError("a new request must be pending")
        require_aware(request.requested_at)
        async with self._scope.write("a request to switch an item on") as conn:
            added = await conn.fetchval(
                _ADD_REQUEST,
                request.id,
                self._tenant,
                request.kind,
                request.item_id,
                request.fingerprint,
                request.requested_by,
                request.requested_at,
            )
            return written(added)

    async def approve(self, request_id: UUID, *, by: str, at: datetime, step_up_at: datetime) -> bool:
        """Approve a pending request and switch its item on, in one transaction. False if not pending.

        The database refuses it without ``step_up_at`` or a person as ``by``, and the switch
        is written for exactly the files that were asked for.
        """
        require_aware(at, step_up_at)
        async with self._scope.write("an approved request and its switch") as conn:
            asked = await conn.fetchrow(_APPROVE, self._tenant, request_id, by, at, step_up_at)
            if not written(asked) or asked is None:  # the second test is for the type checker
                return False
            await conn.execute(
                _SWITCH_ON,
                self._tenant,
                asked["kind"],
                asked["item_id"],
                asked["fingerprint"],
                request_id,
                by,
                at,
            )
        return True

    async def refuse(self, request_id: UUID, *, by: str, at: datetime) -> bool:
        """Refuse a pending request. Returns False if it is unknown or already decided."""
        require_aware(at)
        async with self._scope.write("a refused request") as conn:
            return written(await conn.fetchval(_REFUSE, self._tenant, request_id, by, at))

    async def withdraw(self, request_id: UUID, *, by: str, at: datetime) -> bool:
        """Withdraw a pending request. Returns False if it is unknown or already decided."""
        require_aware(at)
        async with self._scope.write("a withdrawn request") as conn:
            return written(await conn.fetchval(_WITHDRAW, self._tenant, request_id, by, at))

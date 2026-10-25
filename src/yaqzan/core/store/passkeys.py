"""Passkey rows: the public half of the Chairman's passkey (ADR-SEC-007).

Only public data is kept: credential id, public key, signature counter. At
most one passkey is active per tenant (a unique index in the database). The
first is added only while none is active; a lost one is replaced by revoking
it and adding the new one in one transaction, so the old row stays on record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from yaqzan.core.store.db import Scoped, require_aware, written

_ACTIVE = """
SELECT credential_id, public_key, sign_count, created_by, created_at, last_used_at
FROM yaqzan.passkeys WHERE tenant_id = $1 AND revoked_at IS NULL
"""
_ADD = """
INSERT INTO yaqzan.passkeys (tenant_id, credential_id, public_key, sign_count, created_by, created_at)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT DO NOTHING
RETURNING credential_id
"""
_REVOKE = """
UPDATE yaqzan.passkeys SET revoked_at = $2 WHERE tenant_id = $1 AND revoked_at IS NULL
RETURNING credential_id
"""
_USED = """
UPDATE yaqzan.passkeys SET sign_count = $3, last_used_at = $4
WHERE tenant_id = $1 AND credential_id = $2 AND revoked_at IS NULL AND sign_count <= $3
RETURNING credential_id
"""


@dataclass(frozen=True)
class PasskeyRow:
    """One stored passkey (public data only)."""

    credential_id: bytes
    public_key: bytes
    sign_count: int
    created_by: str
    created_at: datetime
    last_used_at: datetime | None = None


class PasskeyRows:
    """Repository for passkeys."""

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope
        self._tenant = scope.tenant_id

    async def active(self) -> PasskeyRow | None:
        """The tenant's one active passkey, or None."""
        async with self._scope.connection() as conn:
            record = await conn.fetchrow(_ACTIVE, self._tenant)
        return None if record is None else PasskeyRow(**dict(record))

    async def add_first(self, row: PasskeyRow) -> bool:
        """Store the first passkey. Returns False if the tenant already has an active one."""
        require_aware(row.created_at)
        async with self._scope.write("the Chairman's first passkey") as conn:
            added = await conn.fetchval(
                _ADD,
                self._tenant,
                row.credential_id,
                row.public_key,
                row.sign_count,
                row.created_by,
                row.created_at,
            )
            return written(added)

    async def replace(self, row: PasskeyRow) -> bool:
        """Revoke the active passkey and add ``row``, all or nothing. False if the new one was not added."""
        require_aware(row.created_at)
        try:
            async with self._scope.write("a replacement passkey") as conn:
                await conn.fetch(_REVOKE, self._tenant, row.created_at)
                added = await conn.fetchval(
                    _ADD,
                    self._tenant,
                    row.credential_id,
                    row.public_key,
                    row.sign_count,
                    row.created_by,
                    row.created_at,
                )
                if added is None:  # e.g. the very same credential again
                    raise _RollbackError
        except _RollbackError:
            return False
        return True

    async def record_use(self, credential_id: bytes, sign_count: int, at: datetime) -> bool:
        """Move the counter forward after a verified use. False if unknown, revoked or going backwards."""
        require_aware(at)
        async with self._scope.write("a passkey's use counter") as conn:
            return written(await conn.fetchval(_USED, self._tenant, credential_id, sign_count, at))


class _RollbackError(Exception):
    """Undo a half-done replacement (the old passkey stays active)."""

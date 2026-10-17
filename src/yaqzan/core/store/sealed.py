"""Sealed credentials: ciphertext bytes only (threat CR-1).

The store never sees a plaintext secret and never imports the vault. It keeps
the vault's output (master key id, wrapped data key, ciphertext) against the
credential's slot. Only the vault, given the master key, can open it.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import asyncpg

from yaqzan.core.store.db import Database

_UPSERT = """
INSERT INTO yaqzan.sealed_secrets (tenant_id, project_id, connector, name, environment, format_version,
    master_key_id, wrapped_dek, ciphertext)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
ON CONFLICT (tenant_id, project_id, connector, name, environment) DO UPDATE SET
    format_version = EXCLUDED.format_version, master_key_id = EXCLUDED.master_key_id,
    wrapped_dek = EXCLUDED.wrapped_dek, ciphertext = EXCLUDED.ciphertext, updated_at = now()
"""
_GET = """
SELECT tenant_id, project_id, connector, name, environment, format_version, master_key_id, wrapped_dek,
    ciphertext
FROM yaqzan.sealed_secrets
WHERE tenant_id = $1 AND project_id = $2 AND connector = $3 AND name = $4 AND environment = $5
"""
_DELETE = """
DELETE FROM yaqzan.sealed_secrets
WHERE tenant_id = $1 AND project_id = $2 AND connector = $3 AND name = $4 AND environment = $5
RETURNING name
"""
_NOT_UNDER_KEY = """
SELECT tenant_id, project_id, connector, name, environment, format_version, master_key_id, wrapped_dek,
    ciphertext
FROM yaqzan.sealed_secrets WHERE tenant_id = $1 AND master_key_id <> $2
ORDER BY project_id, connector, name, environment
"""


@dataclass(frozen=True)
class SlotKey:
    """Where a credential belongs. Mirrors the vault's ``CredentialSlot`` field for field."""

    tenant_id: UUID
    project_id: UUID
    connector: str
    name: str
    environment: str

    def params(self) -> tuple[UUID, UUID, str, str, str]:
        """The slot as query parameters, in column order."""
        return (self.tenant_id, self.project_id, self.connector, self.name, self.environment)


@dataclass(frozen=True)
class SealedRow:
    """One stored credential: a slot and the vault's ciphertexts. No plaintext anywhere."""

    slot: SlotKey
    format_version: int
    master_key_id: str
    wrapped_dek: bytes
    ciphertext: bytes

    def __repr__(self) -> str:
        size = len(self.ciphertext)
        return f"SealedRow(slot={self.slot!r}, key={self.master_key_id!r}, ciphertext=<{size} bytes>)"

    __str__ = __repr__


def _row(record: asyncpg.Record) -> SealedRow:
    fields = dict(record)
    slot = SlotKey(
        **{k: fields.pop(k) for k in ("tenant_id", "project_id", "connector", "name", "environment")}
    )
    return SealedRow(slot=slot, **fields)


class SealedSecrets:
    """Repository for sealed credentials."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def put(self, row: SealedRow) -> None:
        """Store or replace the credential in one slot."""
        async with self._db.connection() as conn:
            await conn.execute(
                _UPSERT,
                *row.slot.params(),
                row.format_version,
                row.master_key_id,
                row.wrapped_dek,
                row.ciphertext,
            )

    async def get(self, slot: SlotKey) -> SealedRow | None:
        """The credential in one slot, or None."""
        async with self._db.connection() as conn:
            record = await conn.fetchrow(_GET, *slot.params())
        return None if record is None else _row(record)

    async def delete(self, slot: SlotKey) -> bool:
        """Remove one credential. Returns False if the slot was empty."""
        async with self._db.connection() as conn:
            return await conn.fetchval(_DELETE, *slot.params()) is not None

    async def not_under_key(self, tenant_id: UUID, master_key_id: str) -> list[SealedRow]:
        """Credentials still wrapped by an older master key (to re-wrap after rotation)."""
        async with self._db.connection() as conn:
            records = await conn.fetch(_NOT_UNDER_KEY, tenant_id, master_key_id)
        return [_row(record) for record in records]

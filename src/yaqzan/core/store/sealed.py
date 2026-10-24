"""Sealed credentials: ciphertext bytes only (threat CR-1).

The store never sees a plaintext secret and never imports the vault. It keeps
the vault's output (master key id, wrapped data key, ciphertext) against the
credential's slot. Only the vault, given the master key, can open it.

Beside the ciphertext sit two plain facts that are not secret: when the key
expires and its access level. :meth:`SealedSecrets.dates` lists them without
touching any ciphertext, for the daily expiry check.

Each slot lives in one workspace, behind its wall (ADR-039). The two
tenant-wide reads (:meth:`SealedSecrets.not_under_key` and
:meth:`SealedSecrets.dates`) go through the tenant's workspaces one at a time;
none is missed, because a credential can only be saved in a workspace that exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import asyncpg

from yaqzan.core.store.db import Database
from yaqzan.core.store.workspaces import WorkspaceRows

_UPSERT = """
INSERT INTO yaqzan.sealed_secrets (tenant_id, project_id, connector, name, environment, format_version,
    master_key_id, wrapped_dek, ciphertext, expires_at, access)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
ON CONFLICT (tenant_id, project_id, connector, name, environment) DO UPDATE SET
    format_version = EXCLUDED.format_version, master_key_id = EXCLUDED.master_key_id,
    wrapped_dek = EXCLUDED.wrapped_dek, ciphertext = EXCLUDED.ciphertext,
    expires_at = EXCLUDED.expires_at, access = EXCLUDED.access, updated_at = now()
"""
_GET = """
SELECT tenant_id, project_id, connector, name, environment, format_version, master_key_id, wrapped_dek,
    ciphertext, expires_at, access
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
    ciphertext, expires_at, access
FROM yaqzan.sealed_secrets WHERE tenant_id = $1 AND project_id = $2 AND master_key_id <> $3
ORDER BY connector, name, environment
"""
_DATES = """
SELECT tenant_id, project_id, connector, name, environment, expires_at, access, updated_at
FROM yaqzan.sealed_secrets WHERE tenant_id = $1 AND project_id = $2
ORDER BY connector, name, environment
"""

Access = Literal["read_only", "read_write", "unknown"]


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
    expires_at: datetime | None = None
    access: Access = "unknown"

    def __repr__(self) -> str:
        size = len(self.ciphertext)
        return f"SealedRow(slot={self.slot!r}, key={self.master_key_id!r}, ciphertext=<{size} bytes>)"

    __str__ = __repr__


@dataclass(frozen=True)
class CredentialDates:
    """A credential's plain facts: where it belongs, when it expires, what it may do. No ciphertext."""

    slot: SlotKey
    expires_at: datetime | None
    access: Access
    updated_at: datetime


def _slot(fields: dict[str, Any]) -> SlotKey:
    """Take the slot columns out of ``fields``, leaving the rest."""
    return SlotKey(
        **{k: fields.pop(k) for k in ("tenant_id", "project_id", "connector", "name", "environment")}
    )


def _row(record: asyncpg.Record) -> SealedRow:
    fields = dict(record)
    return SealedRow(slot=_slot(fields), **fields)


class SealedSecrets:
    """Repository for sealed credentials."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def put(self, row: SealedRow) -> None:
        """Store or replace the credential in one slot."""
        async with self._db.scoped(row.slot.tenant_id, row.slot.project_id).write("a sealed secret") as conn:
            await conn.execute(
                _UPSERT,
                *row.slot.params(),
                row.format_version,
                row.master_key_id,
                row.wrapped_dek,
                row.ciphertext,
                row.expires_at,
                row.access,
            )

    async def get(self, slot: SlotKey) -> SealedRow | None:
        """The credential in one slot, or None."""
        async with self._db.scoped(slot.tenant_id, slot.project_id).connection() as conn:
            record = await conn.fetchrow(_GET, *slot.params())
        return None if record is None else _row(record)

    async def delete(self, slot: SlotKey) -> bool:
        """Remove one credential. Returns False if the slot was empty."""
        where = self._db.scoped(slot.tenant_id, slot.project_id)
        async with where.write("the removal of a sealed secret") as conn:
            return await conn.fetchval(_DELETE, *slot.params()) is not None

    async def not_under_key(self, tenant_id: UUID, master_key_id: str) -> list[SealedRow]:
        """Credentials still wrapped by an older master key (to re-wrap after rotation)."""
        out: list[SealedRow] = []
        for workspace_id in await self._workspace_ids(tenant_id):
            async with self._db.scoped(tenant_id, workspace_id).connection() as conn:
                records = await conn.fetch(_NOT_UNDER_KEY, tenant_id, workspace_id, master_key_id)
            out.extend(_row(record) for record in records)
        return out

    async def dates(self, tenant_id: UUID) -> list[CredentialDates]:
        """Every credential's expiry and access, without reading any ciphertext."""
        out = []
        for workspace_id in await self._workspace_ids(tenant_id):
            async with self._db.scoped(tenant_id, workspace_id).connection() as conn:
                records = await conn.fetch(_DATES, tenant_id, workspace_id)
            for record in records:
                fields = dict(record)
                out.append(CredentialDates(slot=_slot(fields), **fields))
        return out

    async def _workspace_ids(self, tenant_id: UUID) -> list[UUID]:
        """The tenant's workspaces in a fixed order (by id), so results come out in a fixed order too."""
        return sorted(w.workspace_id for w in await WorkspaceRows(self._db).workspaces(tenant_id))

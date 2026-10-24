"""Sealed credentials: ciphertext bytes only (threat CR-1).

The store never sees a plaintext secret and never imports the vault. It keeps
the vault's output (master key id, wrapped data key, ciphertext) against the
credential's slot. Only the vault, given the master key, can open it.

Beside the ciphertext sit two plain facts that are not secret: when the key
expires and its access level. :meth:`AllSealedSecrets.dates` lists them without
touching any ciphertext, for the daily expiry check.

Each slot lives in one workspace, behind its wall (ADR-039). :class:`SealedSecrets`
works in one workspace; :class:`AllSealedSecrets` does the two tenant-wide reads
one workspace at a time; none is missed, because a credential can only be saved
in a workspace that exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import asyncpg

from yaqzan.core.store.db import Scoped
from yaqzan.core.store.errors import StoreError, StoreErrorCode
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
    """Repository for the sealed credentials of the one workspace of its handle.

    A slot of another workspace or tenant is refused before any query runs.
    """

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope
        self._key = (scope.tenant_id, scope.require_workspace())

    def _check(self, slot: SlotKey) -> None:
        if (slot.tenant_id, slot.project_id) != self._key:
            raise StoreError(StoreErrorCode.DENIED, "a credential slot of another workspace")

    async def put(self, row: SealedRow) -> None:
        """Store or replace the credential in one slot."""
        self._check(row.slot)
        async with self._scope.write("a sealed secret") as conn:
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
        self._check(slot)
        async with self._scope.connection() as conn:
            record = await conn.fetchrow(_GET, *slot.params())
        return None if record is None else _row(record)

    async def delete(self, slot: SlotKey) -> bool:
        """Remove one credential. Returns False if the slot was empty."""
        self._check(slot)
        async with self._scope.write("the removal of a sealed secret") as conn:
            return await conn.fetchval(_DELETE, *slot.params()) is not None


class AllSealedSecrets:
    """The tenant-wide reads of credentials, done one workspace at a time behind each wall.

    None is missed: a credential can only be saved in a workspace that exists.
    """

    def __init__(self, scope: Scoped) -> None:
        self._scope = scope

    async def not_under_key(self, master_key_id: str) -> list[SealedRow]:
        """Credentials still wrapped by an older master key (to re-wrap after rotation)."""
        out: list[SealedRow] = []
        for where in await self._each_workspace():
            async with where.connection() as conn:
                records = await conn.fetch(_NOT_UNDER_KEY, where.tenant_id, where.workspace_id, master_key_id)
            out.extend(_row(record) for record in records)
        return out

    async def dates(self) -> list[CredentialDates]:
        """Every credential's expiry and access, without reading any ciphertext."""
        out = []
        for where in await self._each_workspace():
            async with where.connection() as conn:
                records = await conn.fetch(_DATES, where.tenant_id, where.workspace_id)
            for record in records:
                fields = dict(record)
                out.append(CredentialDates(slot=_slot(fields), **fields))
        return out

    async def _each_workspace(self) -> list[Scoped]:
        """A handle per workspace in a fixed order (by id), so results come out in a fixed order too."""
        ids = sorted(w.workspace_id for w in await WorkspaceRows(self._scope).workspaces())
        return [self._scope.within(workspace_id) for workspace_id in ids]

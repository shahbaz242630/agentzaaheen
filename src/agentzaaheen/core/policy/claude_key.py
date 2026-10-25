"""The Claude API key: sealed in the vault after Windows Hello, opened for one call at a time (ADR-033).

The key lives in the Agent Zaaheen workspace (the harness's own project; the
Chairman's choice, 25 Oct 2026) in the slot ``claude`` / ``api_key``. It is never
kept in a file or an environment variable.

Saving needs the Chairman's passkey to sign exactly this save: the challenge's
purpose names the workspace and a short hash of the key, so a signature made for
one key cannot save another. The database keeps only the vault's ciphertext; the
key is never logged, audited or shown again. Every save and every refusal is
audited (never the key).

This layer decides; it never touches the vault. Sealing and opening are done by
the broker's :class:`~agentzaaheen.core.broker.SealedSlot`, handed in as a
:class:`KeySlot`, so the web layer that uses this cannot reach the vault even
indirectly. Opening (:meth:`ClaudeKeyControl.key_source`) reads the key fresh for
each call, so a replaced key is used at once and nothing keeps a copy.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import SecretStr

from agentzaaheen.core.auth import PasskeyError
from agentzaaheen.core.policy.audit import AuditDraft, AuditOutcome
from agentzaaheen.core.policy.ledger import Ledger
from agentzaaheen.core.policy.passkey_approvals import CHAIRMAN, PasskeyApprovals
from agentzaaheen.core.sdk import get_logger, say
from agentzaaheen.core.store import AllSealedSecrets, Database

CONNECTOR = "claude"
NAME = "api_key"
KEY_PREFIX = "sk-ant-"
MAX_KEY_CHARS = 512
_HASH_CHARS = 16  # 64 bits of SHA-256: enough to tell keys apart, far too little to learn anything


class ClaudeKeyVerdict(StrEnum):
    """How a save ended. Safe to log and show."""

    SAVED = "claude_key.saved"
    NEEDS_PASSKEY = "claude_key.needs_passkey"  # sign this exact save with Windows Hello, then send it again
    PASSKEY_FAILED = "claude_key.passkey_failed"
    BAD_KEY = "claude_key.bad_key"
    NO_VAULT = "claude_key.no_vault"  # the vault's master key file was not found at start


class KeySlot(Protocol):
    """One sealed credential slot (the broker's ``SealedSlot`` in real use)."""

    @property
    def ready(self) -> bool:
        """Whether the vault can seal and open (its master key file was found)."""
        ...

    async def save(self, secret: SecretStr) -> None:
        """Seal ``secret`` into the slot, replacing any older one."""
        ...

    async def open(self) -> SecretStr:
        """The secret for one use; raises ``SourceError`` when it cannot be opened."""
        ...


def key_problem(raw: str) -> str | None:
    """Why ``raw`` cannot be a Claude API key, in a few words; ``None`` if it may be one."""
    if not raw:
        return "it is empty"
    if len(raw) > MAX_KEY_CHARS:
        return "it is too long"
    if not raw.isascii() or not raw.isprintable() or " " in raw:
        return "it contains spaces or control characters"
    if not raw.startswith(KEY_PREFIX):
        return f"Claude API keys start with {KEY_PREFIX}"
    return None


def purpose(workspace_id: UUID, key: SecretStr) -> str:
    """The challenge's purpose: names this save of this key, so its signature fits nothing else."""
    digest = hashlib.sha256(key.get_secret_value().encode("utf-8")).hexdigest()[:_HASH_CHARS]
    return f"step:claude-key:{workspace_id}:{digest}"


@dataclass(frozen=True)
class ClaudeKeyStatus:
    """What the Customize page shows: never the key, only whether and when it was saved."""

    vault: bool
    saved: bool
    saved_at: datetime | None


class ClaudeKeyControl:
    """Save the Claude key (with a passkey step-up) and open it for one call. Acts as the Chairman."""

    def __init__(  # noqa: PLR0913  # the database, whose key, its slot, the passkey, the time
        self,
        db: Database,
        tenant_id: UUID,
        workspace_id: UUID,
        *,
        slot: KeySlot,
        passkeys: PasskeyApprovals,
        clock: Callable[[], datetime],
    ) -> None:
        self._tenant = db.scoped(tenant_id)
        self._ledger = Ledger(db, tenant_id)
        self._workspace = workspace_id
        self._slot = slot
        self._passkeys = passkeys
        self._clock = clock

    async def status(self) -> ClaudeKeyStatus:
        """Whether the vault is ready and a key is saved (read without opening it)."""
        saved = [
            cred.updated_at
            for cred in await AllSealedSecrets(self._tenant).dates()
            if (cred.slot.project_id, cred.slot.connector, cred.slot.name)
            == (self._workspace, CONNECTOR, NAME)
        ]
        return ClaudeKeyStatus(vault=self._slot.ready, saved=bool(saved), saved_at=max(saved, default=None))

    async def options(self, key: SecretStr) -> dict[str, Any]:
        """Options for Windows Hello to sign saving exactly ``key``; refused first if bad or no vault."""
        if not self._slot.ready:
            raise PasskeyError(ClaudeKeyVerdict.NO_VAULT.value)
        if key_problem(key.get_secret_value()) is not None:
            raise PasskeyError(ClaudeKeyVerdict.BAD_KEY.value)
        return await self._passkeys.step_up_options(purpose(self._workspace, key))

    async def save(self, key: SecretStr, credential: dict[str, Any] | None) -> ClaudeKeyVerdict:
        """Seal ``key`` once Windows Hello has signed this exact save; audited either way (never the key)."""
        now = self._clock()
        if not self._slot.ready:
            return await self._decided(ClaudeKeyVerdict.NO_VAULT, now)
        if key_problem(key.get_secret_value()) is not None:
            return await self._decided(ClaudeKeyVerdict.BAD_KEY, now)
        if credential is None:
            return ClaudeKeyVerdict.NEEDS_PASSKEY
        if await self._passkeys.step_up(purpose(self._workspace, key), credential) is None:
            return await self._decided(ClaudeKeyVerdict.PASSKEY_FAILED, now)
        await self._slot.save(key)
        return await self._decided(ClaudeKeyVerdict.SAVED, now)

    def key_source(self) -> Callable[[], Awaitable[SecretStr]]:
        """The Claude door's key source: the sealed key, opened for one call (fails closed)."""
        return self._slot.open

    async def _decided(self, verdict: ClaudeKeyVerdict, now: datetime) -> ClaudeKeyVerdict:
        say(get_logger("agentzaaheen.claude_key"), "claude_key_decided", verdict=verdict.value)
        await self._ledger.record(
            AuditDraft(
                at=now,
                actor=CHAIRMAN,
                action="claude_key.save",
                outcome=AuditOutcome.ALLOW if verdict is ClaudeKeyVerdict.SAVED else AuditOutcome.DENY,
                reason_code=verdict.value,
                correlation_id=uuid4(),
                project_id=self._workspace,
                target_ref=f"credential:{CONNECTOR}:{NAME}",
            )
        )
        return verdict

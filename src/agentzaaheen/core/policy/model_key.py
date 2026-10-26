"""Our server model's own API key: sealed after Windows Hello, opened for each call through the tunnel.

The server's llama-server answers only calls that carry its key
(``--api-key-file``), behind Cloudflare Access (the service token, see
:mod:`~agentzaaheen.core.policy.server_token`). So a call from the laptop needs
both: Cloudflare's token gets it through the tunnel, this key gets it answered.
Like the Claude key, it lives in the Agent Zaaheen workspace, in its own slot
``server_model`` / ``api_key``, and never in a file or an environment variable.

Saving needs the Chairman's passkey to sign exactly this save: the challenge's
purpose names the workspace and a short hash of the key, so a signature made for
one key cannot save another. The key is never logged, audited or shown again;
every save and every refusal is audited (never the key).

This layer decides; it never touches the vault. Sealing and opening are done by
the broker's slot, handed in as a :class:`KeySlot`.
"""

from __future__ import annotations

import hashlib
import string
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import SecretStr

from agentzaaheen.core.auth import PasskeyError
from agentzaaheen.core.policy.audit import AuditDraft, AuditOutcome
from agentzaaheen.core.policy.claude_key import KEY_PREFIX as CLAUDE_KEY_PREFIX
from agentzaaheen.core.policy.claude_key import KeySlot
from agentzaaheen.core.policy.ledger import Ledger
from agentzaaheen.core.policy.passkey_approvals import CHAIRMAN, PasskeyApprovals
from agentzaaheen.core.sdk import SourceError, SourceErrorCode, get_logger, say
from agentzaaheen.core.store import AllSealedSecrets, Database

CONNECTOR = "server_model"
NAME = "api_key"
MIN_KEY_CHARS = 32
MAX_KEY_CHARS = 256
_ALLOWED = frozenset(string.ascii_letters + string.digits + "-_")
_HASH_CHARS = 16  # 64 bits of SHA-256: enough to tell keys apart, far too little to learn anything


class ModelKeyVerdict(StrEnum):
    """How a save ended. Safe to log and show."""

    SAVED = "model_key.saved"
    NEEDS_PASSKEY = "model_key.needs_passkey"  # sign this exact save with Windows Hello, then send it again
    PASSKEY_FAILED = "model_key.passkey_failed"
    BAD_KEY = "model_key.bad_key"
    NO_VAULT = "model_key.no_vault"  # the vault's master key file was not found at start


def model_key_problem(raw: str) -> str | None:
    """Why ``raw`` cannot be our server model's key, in a few words; ``None`` if it may be one."""
    if not raw:
        return "it is empty"
    if len(raw) < MIN_KEY_CHARS:
        return "it is too short"
    if len(raw) > MAX_KEY_CHARS:
        return "it is too long"
    if not set(raw) <= _ALLOWED:
        return "it contains spaces or other characters the key never has"
    if raw.startswith(CLAUDE_KEY_PREFIX):
        return "that is a Claude key, not the server's"
    return None


def purpose(workspace_id: UUID, key: SecretStr) -> str:
    """The challenge's purpose: names this save of this key, so its signature fits nothing else."""
    digest = hashlib.sha256(key.get_secret_value().encode("utf-8")).hexdigest()[:_HASH_CHARS]
    return f"step:model-key:{workspace_id}:{digest}"


@dataclass(frozen=True)
class ModelKeyStatus:
    """What the Customize page shows: never the key, only whether and when it was saved."""

    vault: bool
    saved: bool
    saved_at: datetime | None


class ModelKeyControl:
    """Save the server model's key (with a passkey step-up) and open it for one call. Acts as the Chairman."""

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

    async def status(self) -> ModelKeyStatus:
        """Whether the vault is ready and a key is saved (read without opening it)."""
        saved = [
            cred.updated_at
            for cred in await AllSealedSecrets(self._tenant).dates()
            if (cred.slot.project_id, cred.slot.connector, cred.slot.name)
            == (self._workspace, CONNECTOR, NAME)
        ]
        return ModelKeyStatus(vault=self._slot.ready, saved=bool(saved), saved_at=max(saved, default=None))

    async def options(self, key: SecretStr) -> dict[str, Any]:
        """Options for Windows Hello to sign saving exactly ``key``; refused first if bad or no vault."""
        if not self._slot.ready:
            raise PasskeyError(ModelKeyVerdict.NO_VAULT.value)
        if model_key_problem(key.get_secret_value()) is not None:
            raise PasskeyError(ModelKeyVerdict.BAD_KEY.value)
        return await self._passkeys.step_up_options(purpose(self._workspace, key))

    async def save(self, key: SecretStr, credential: dict[str, Any] | None) -> ModelKeyVerdict:
        """Seal ``key`` once Windows Hello has signed this exact save; audited either way (never the key)."""
        now = self._clock()
        if not self._slot.ready:
            return await self._decided(ModelKeyVerdict.NO_VAULT, now)
        if model_key_problem(key.get_secret_value()) is not None:
            return await self._decided(ModelKeyVerdict.BAD_KEY, now)
        if credential is None:
            return ModelKeyVerdict.NEEDS_PASSKEY
        if await self._passkeys.step_up(purpose(self._workspace, key), credential) is None:
            return await self._decided(ModelKeyVerdict.PASSKEY_FAILED, now)
        await self._slot.save(key)
        return await self._decided(ModelKeyVerdict.SAVED, now)

    def key_source(self) -> Callable[[], Awaitable[SecretStr]]:
        """The model client's key source: the sealed key, opened for one call (fails closed)."""

        async def opened() -> SecretStr:
            key = await self._slot.open()
            if model_key_problem(key.get_secret_value()) is not None:
                raise SourceError(SourceErrorCode.BAD_RESPONSE, "the sealed server model key is not a key")
            return key

        return opened

    async def _decided(self, verdict: ModelKeyVerdict, now: datetime) -> ModelKeyVerdict:
        say(get_logger("agentzaaheen.model_key"), "model_key_decided", verdict=verdict.value)
        await self._ledger.record(
            AuditDraft(
                at=now,
                actor=CHAIRMAN,
                action="model_key.save",
                outcome=AuditOutcome.ALLOW if verdict is ModelKeyVerdict.SAVED else AuditOutcome.DENY,
                reason_code=verdict.value,
                correlation_id=uuid4(),
                project_id=self._workspace,
                target_ref=f"credential:{CONNECTOR}:{NAME}",
            )
        )
        return verdict

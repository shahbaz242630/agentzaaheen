"""The laptop's Cloudflare service token: sealed after Windows Hello, opened for each call to our server.

Our server's model is reached through the Cloudflare tunnel at
``llm.agentzaaheen.com``, and Cloudflare Access refuses every request that does
not carry this token (two parts: a Client ID and a Client Secret). Like the
Claude key, it lives in the Agent Zaaheen workspace, in the slot
``cloudflare_access`` / ``service_token``, and never in a file or an
environment variable.

Saving needs the Chairman's passkey to sign exactly this save: the challenge's
purpose names the workspace and a short hash of both parts, so a signature made
for one token cannot save another. Both parts are sealed together, so one can
never be replaced without the other. They are never logged, audited or shown
again; every save and every refusal is audited (never the token).

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
from agentzaaheen.core.policy.claude_key import KeySlot
from agentzaaheen.core.policy.ledger import Ledger
from agentzaaheen.core.policy.passkey_approvals import CHAIRMAN, PasskeyApprovals
from agentzaaheen.core.sdk import SourceError, SourceErrorCode, get_logger, say
from agentzaaheen.core.store import AllSealedSecrets, Database

CONNECTOR = "cloudflare_access"
NAME = "service_token"
ID_SUFFIX = ".access"
MAX_PART_CHARS = 256
_SEPARATOR = "\n"  # neither part may contain it, so the sealed pair splits back exactly
_ALLOWED = frozenset(string.ascii_letters + string.digits + "-_.")
_HASH_CHARS = 16  # 64 bits of SHA-256: enough to tell tokens apart, far too little to learn anything


class ServerTokenVerdict(StrEnum):
    """How a save ended. Safe to log and show."""

    SAVED = "server_token.saved"
    NEEDS_PASSKEY = "server_token.needs_passkey"  # sign this save with Windows Hello, then send it again
    PASSKEY_FAILED = "server_token.passkey_failed"
    BAD_TOKEN = "server_token.bad_token"  # noqa: S105  # nosec B105  # a verdict code, not a secret
    NO_VAULT = "server_token.no_vault"  # the vault's master key file was not found at start


@dataclass(frozen=True)
class ServiceToken:
    """Both parts of the token, for one call. The secret stays hidden in logs and reprs."""

    client_id: str
    client_secret: SecretStr


def token_problem(client_id: str, client_secret: str) -> str | None:
    """Why these cannot be a Cloudflare service token, in a few words; ``None`` if they may be one."""
    for part in (client_id, client_secret):
        if not part:
            return "a part is empty"
        if len(part) > MAX_PART_CHARS:
            return "a part is too long"
        if not set(part) <= _ALLOWED:
            return "a part contains spaces or other characters a token never has"
    if not client_id.endswith(ID_SUFFIX) or client_id == ID_SUFFIX:
        return f"the Client ID ends with {ID_SUFFIX}"
    if client_secret.endswith(ID_SUFFIX):
        return "the two parts look swapped"
    return None


def purpose(workspace_id: UUID, client_id: str, client_secret: SecretStr) -> str:
    """The challenge's purpose: names this save of this token, so its signature fits nothing else."""
    pair = _sealed(client_id, client_secret).get_secret_value().encode("utf-8")
    return f"step:server-token:{workspace_id}:{hashlib.sha256(pair).hexdigest()[:_HASH_CHARS]}"


def _sealed(client_id: str, client_secret: SecretStr) -> SecretStr:
    return SecretStr(client_id + _SEPARATOR + client_secret.get_secret_value())


@dataclass(frozen=True)
class ServerTokenStatus:
    """What the Customize page shows: never the token, only whether and when it was saved."""

    vault: bool
    saved: bool
    saved_at: datetime | None


class ServerTokenControl:
    """Save the service token (with a passkey step-up) and open it for one call. Acts as the Chairman."""

    def __init__(  # noqa: PLR0913  # the database, whose token, its slot, the passkey, the time
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

    async def status(self) -> ServerTokenStatus:
        """Whether the vault is ready and a token is saved (read without opening it)."""
        saved = [
            cred.updated_at
            for cred in await AllSealedSecrets(self._tenant).dates()
            if (cred.slot.project_id, cred.slot.connector, cred.slot.name)
            == (self._workspace, CONNECTOR, NAME)
        ]
        return ServerTokenStatus(vault=self._slot.ready, saved=bool(saved), saved_at=max(saved, default=None))

    async def options(self, client_id: str, client_secret: SecretStr) -> dict[str, Any]:
        """Options for Windows Hello to sign saving exactly this token; refused first if bad or no vault."""
        if not self._slot.ready:
            raise PasskeyError(ServerTokenVerdict.NO_VAULT.value)
        if token_problem(client_id, client_secret.get_secret_value()) is not None:
            raise PasskeyError(ServerTokenVerdict.BAD_TOKEN.value)
        return await self._passkeys.step_up_options(purpose(self._workspace, client_id, client_secret))

    async def save(
        self, client_id: str, client_secret: SecretStr, credential: dict[str, Any] | None
    ) -> ServerTokenVerdict:
        """Seal the token once Windows Hello has signed this exact save; audited either way (never it)."""
        now = self._clock()
        if not self._slot.ready:
            return await self._decided(ServerTokenVerdict.NO_VAULT, now)
        if token_problem(client_id, client_secret.get_secret_value()) is not None:
            return await self._decided(ServerTokenVerdict.BAD_TOKEN, now)
        if credential is None:
            return ServerTokenVerdict.NEEDS_PASSKEY
        signed = purpose(self._workspace, client_id, client_secret)
        if await self._passkeys.step_up(signed, credential) is None:
            return await self._decided(ServerTokenVerdict.PASSKEY_FAILED, now)
        await self._slot.save(_sealed(client_id, client_secret))
        return await self._decided(ServerTokenVerdict.SAVED, now)

    def token_source(self) -> Callable[[], Awaitable[ServiceToken]]:
        """The model client's token source: the sealed token, opened for one call (fails closed)."""

        async def opened() -> ServiceToken:
            client_id, _, client_secret = (await self._slot.open()).get_secret_value().partition(_SEPARATOR)
            if token_problem(client_id, client_secret) is not None:
                raise SourceError(SourceErrorCode.BAD_RESPONSE, "the sealed server token is not a token")
            return ServiceToken(client_id, SecretStr(client_secret))

        return opened

    async def _decided(self, verdict: ServerTokenVerdict, now: datetime) -> ServerTokenVerdict:
        say(get_logger("agentzaaheen.server_token"), "server_token_decided", verdict=verdict.value)
        await self._ledger.record(
            AuditDraft(
                at=now,
                actor=CHAIRMAN,
                action="server_token.save",
                outcome=AuditOutcome.ALLOW if verdict is ServerTokenVerdict.SAVED else AuditOutcome.DENY,
                reason_code=verdict.value,
                correlation_id=uuid4(),
                project_id=self._workspace,
                target_ref=f"credential:{CONNECTOR}:{NAME}",
            )
        )
        return verdict

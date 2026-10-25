"""Save and open credentials: the vault's encryption joined to the store's rows.

Both work in the one workspace of the caller's handle; a slot of another
workspace is refused (ADR-039). Saving seals the key and stores only ciphertext, plus two plain facts: when
it expires and its access level. Opening returns a ``SecretStr`` for one use;
the caller hands it to a client's secret headers and keeps no copy.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import SecretStr

from agentzaaheen.core.sdk import SourceError, SourceErrorCode
from agentzaaheen.core.store import Access, Scoped, SealedRow, SealedSecrets, SlotKey, StoreError
from agentzaaheen.core.vault import (
    CredentialSlot,
    Keyring,
    SealedSecret,
    VaultError,
    VaultErrorCode,
    open_secret,
    seal,
)


class BrokerErrorCode(StrEnum):
    """Why the broker could not hand out a credential. Safe to log and show."""

    MISSING = "credential.missing"


class BrokerError(Exception):
    """No usable credential. The message never carries a secret."""

    def __init__(self, code: BrokerErrorCode, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code


def _key(slot: CredentialSlot) -> SlotKey:
    return SlotKey(**slot.model_dump(mode="python"))


async def save_credential(  # noqa: PLR0913  # where, what, and the two plain facts
    scope: Scoped,
    keyring: Keyring,
    slot: CredentialSlot,
    secret: SecretStr,
    *,
    expires_at: datetime | None,
    access: Access,
) -> None:
    """Seal ``secret`` for ``slot`` and store it, replacing any older key in that slot."""
    if expires_at is not None and expires_at.tzinfo is None:
        raise ValueError("expires_at must be timezone-aware")
    sealed = seal(secret, slot, keyring)
    await SealedSecrets(scope).put(
        SealedRow(
            slot=_key(slot),
            format_version=sealed.format_version,
            master_key_id=sealed.master_key_id,
            wrapped_dek=sealed.wrapped_dek,
            ciphertext=sealed.ciphertext,
            expires_at=expires_at,
            access=access,
        )
    )


async def open_credential(scope: Scoped, keyring: Keyring, slot: CredentialSlot) -> SecretStr:
    """The key in ``slot`` for one use. Fails closed: missing, tampered or wrong-slot keys never open."""
    row = await SealedSecrets(scope).get(_key(slot))
    if row is None:
        raise BrokerError(BrokerErrorCode.MISSING, f"no {slot.connector} credential is saved")
    sealed = SealedSecret(  # format 1 is the only one; a database CHECK refuses any other
        master_key_id=row.master_key_id,
        wrapped_dek=row.wrapped_dek,
        ciphertext=row.ciphertext,
    )
    return open_secret(sealed, slot, keyring)


class SealedSlot:
    """One credential slot, sealed and opened here, for a caller that may not see the vault.

    ``keyring`` is ``None`` when the vault's master key file was not found: then
    nothing can be saved, and opening fails closed. Keys saved here never expire
    on their own (``expires_at`` is left empty).
    """

    def __init__(
        self, scope: Scoped, keyring: Keyring | None, slot: CredentialSlot, *, access: Access
    ) -> None:
        self._scope = scope
        self._keyring = keyring
        self._slot = slot
        self._access: Access = access

    @property
    def ready(self) -> bool:
        """Whether the vault can seal and open (its master key file was found)."""
        return self._keyring is not None

    async def save(self, secret: SecretStr) -> None:
        """Seal ``secret`` into the slot, replacing any older one."""
        if self._keyring is None:
            raise VaultError(VaultErrorCode.BAD_KEY_FILE, "the vault's master key file was not found")
        await save_credential(
            self._scope, self._keyring, self._slot, secret, expires_at=None, access=self._access
        )

    async def open(self) -> SecretStr:
        """The secret for one use.

        Fails closed as a ``SourceError`` with the reason, so a call that needs it
        ends as "could not be reached", never as a crash and never with a guess.
        """
        try:
            if self._keyring is None:
                raise VaultError(VaultErrorCode.BAD_KEY_FILE, "the vault's master key file was not found")
            return await open_credential(self._scope, self._keyring, self._slot)
        except (BrokerError, VaultError) as exc:
            raise SourceError(
                SourceErrorCode.AUTH,
                f"the {self._slot.connector} key could not be opened",
                detail_code=exc.code,
            ) from None
        except StoreError as exc:
            raise SourceError(
                SourceErrorCode.UNAVAILABLE,
                f"the sealed {self._slot.connector} key could not be read",
                detail_code=exc.code,
            ) from None

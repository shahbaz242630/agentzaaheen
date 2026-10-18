"""Save and open credentials: the vault's encryption joined to the store's rows.

Saving seals the key and stores only ciphertext, plus two plain facts: when
it expires and its access level. Opening returns a ``SecretStr`` for one use;
the caller hands it to a client's secret headers and keeps no copy.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import SecretStr

from yaqzan.core.store import Access, Database, SealedRow, SealedSecrets, SlotKey
from yaqzan.core.vault import CredentialSlot, Keyring, SealedSecret, open_secret, seal


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
    db: Database,
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
    await SealedSecrets(db).put(
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


async def open_credential(db: Database, keyring: Keyring, slot: CredentialSlot) -> SecretStr:
    """The key in ``slot`` for one use. Fails closed: missing, tampered or wrong-slot keys never open."""
    row = await SealedSecrets(db).get(_key(slot))
    if row is None:
        raise BrokerError(BrokerErrorCode.MISSING, f"no {slot.connector} credential is saved")
    sealed = SealedSecret(  # format 1 is the only one; a database CHECK refuses any other
        master_key_id=row.master_key_id,
        wrapped_dek=row.wrapped_dek,
        ciphertext=row.ciphertext,
    )
    return open_secret(sealed, slot, keyring)

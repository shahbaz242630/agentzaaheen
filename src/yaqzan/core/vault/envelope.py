"""Envelope encryption for credentials (SECURITY §5.1, threat CR-1).

Each secret gets its own random 256-bit data key (DEK). The secret is encrypted
with the DEK, and the DEK is encrypted ("wrapped") with a master key. The
database only ever holds the two ciphertexts and the master key's id.

Both layers use AES-256-GCM from the ``cryptography`` library (no DIY crypto,
SP-7) with a fresh 96-bit random nonce each time. Both are bound to the
credential's slot (tenant, project, connector, name, environment) as associated
data, so a ciphertext copied into another project's row will not decrypt.
"""

from __future__ import annotations

import json
import os
from typing import Literal
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from yaqzan.core.vault.errors import VaultError, VaultErrorCode
from yaqzan.core.vault.keys import KEY_BYTES, Keyring, MasterKey

MAX_SECRET_BYTES = 16 * 1024
_NONCE_BYTES = 12
_TAG_BYTES = 16
_DEK_DOMAIN = b"yaqzan.vault.dek.v1|"
_DATA_DOMAIN = b"yaqzan.vault.data.v1|"


class CredentialSlot(BaseModel):
    """Where a credential belongs. One credential per connector per project per environment (SP-3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    project_id: UUID
    connector: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    environment: Literal["production", "staging"]

    def associated_data(self) -> bytes:
        """Canonical bytes that bind a ciphertext to this slot."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")


class SealedSecret(BaseModel):
    """What the database stores for one credential. Contains no plaintext."""

    model_config = ConfigDict(frozen=True, extra="forbid", ser_json_bytes="base64", val_json_bytes="base64")

    format_version: Literal[1] = 1
    master_key_id: str
    wrapped_dek: bytes = Field(min_length=_NONCE_BYTES + KEY_BYTES + _TAG_BYTES)
    ciphertext: bytes = Field(min_length=_NONCE_BYTES + _TAG_BYTES + 1)

    def __repr__(self) -> str:
        size = len(self.ciphertext)
        return f"SealedSecret(master_key_id={self.master_key_id!r}, ciphertext=<{size} bytes>)"

    __str__ = __repr__


def _encrypt(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def _decrypt(key: bytes, blob: bytes, aad: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], aad)
    except InvalidTag as exc:
        raise VaultError(VaultErrorCode.TAMPERED, "ciphertext failed authentication") from exc


def _unwrap_dek(sealed: SealedSecret, slot: CredentialSlot, keyring: Keyring) -> bytes:
    master = keyring.get(sealed.master_key_id)
    return _decrypt(master.material(), sealed.wrapped_dek, _DEK_DOMAIN + slot.associated_data())


def _wrap_dek(dek: bytes, slot: CredentialSlot, master: MasterKey) -> bytes:
    return _encrypt(master.material(), dek, _DEK_DOMAIN + slot.associated_data())


def seal(secret: SecretStr, slot: CredentialSlot, keyring: Keyring) -> SealedSecret:
    """Encrypt ``secret`` for ``slot`` with a fresh data key wrapped by the current master key."""
    plaintext = secret.get_secret_value().encode("utf-8")
    if not plaintext:
        raise VaultError(VaultErrorCode.EMPTY, "refusing to store an empty secret")
    if len(plaintext) > MAX_SECRET_BYTES:
        raise VaultError(VaultErrorCode.TOO_LARGE, f"secret is larger than {MAX_SECRET_BYTES} bytes")
    dek = AESGCM.generate_key(bit_length=256)
    master = keyring.current
    return SealedSecret(
        master_key_id=master.key_id,
        wrapped_dek=_wrap_dek(dek, slot, master),
        ciphertext=_encrypt(dek, plaintext, _DATA_DOMAIN + slot.associated_data()),
    )


def open_secret(sealed: SealedSecret, slot: CredentialSlot, keyring: Keyring) -> SecretStr:
    """Decrypt a sealed secret. Fails closed if anything was changed or the slot differs."""
    dek = _unwrap_dek(sealed, slot, keyring)
    plaintext = _decrypt(dek, sealed.ciphertext, _DATA_DOMAIN + slot.associated_data())
    try:
        return SecretStr(plaintext.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise VaultError(VaultErrorCode.UNSUPPORTED, "secret is not valid UTF-8") from exc


def rewrap(sealed: SealedSecret, slot: CredentialSlot, keyring: Keyring) -> SealedSecret:
    """Re-wrap the data key under the current master key (master-key rotation, SP-8).

    The secret's own ciphertext is unchanged; only the small wrapped key moves.
    """
    if sealed.master_key_id == keyring.current_id:
        return sealed
    dek = _unwrap_dek(sealed, slot, keyring)
    return sealed.model_copy(
        update={
            "master_key_id": keyring.current_id,
            "wrapped_dek": _wrap_dek(dek, slot, keyring.current),
        }
    )

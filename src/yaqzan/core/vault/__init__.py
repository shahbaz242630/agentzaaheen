"""The vault: encrypted credentials. Only the broker may decrypt.

What it does:
    Envelope encryption (a fresh data key per secret, wrapped by a master key
    kept in a root-only file), bound to the credential's slot so ciphertexts
    cannot be swapped between projects; master-key rotation by re-wrapping.
    Name fingerprints (``NamePrinter``): a keyed code for each customer name,
    so the customer-data check can know names Agent Zaaheen never keeps.

Public interface:
    ``seal``, ``open_secret``, ``rewrap``, ``CredentialSlot``, ``SealedSecret``,
    ``MasterKey``, ``Keyring``, ``load_master_key``, ``VaultError``,
    ``VaultErrorCode``, ``NamePrinter``, ``PRINT_BYTES``. Sealed secrets are stored by the
    store module.

How to test:
    ``uv run pytest tests/unit/test_vault.py``

Must never:
    Hand a secret to an agent, a prompt or a log line; read a key from an
    environment variable; store a master key next to the data it protects.
"""

from yaqzan.core.vault.envelope import CredentialSlot, SealedSecret, open_secret, rewrap, seal
from yaqzan.core.vault.errors import VaultError, VaultErrorCode
from yaqzan.core.vault.keys import Keyring, MasterKey, load_master_key
from yaqzan.core.vault.prints import PRINT_BYTES, NamePrinter

__all__ = [
    "PRINT_BYTES",
    "CredentialSlot",
    "Keyring",
    "MasterKey",
    "NamePrinter",
    "SealedSecret",
    "VaultError",
    "VaultErrorCode",
    "load_master_key",
    "open_secret",
    "rewrap",
    "seal",
]

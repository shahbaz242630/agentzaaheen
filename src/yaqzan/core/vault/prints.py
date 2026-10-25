"""Name fingerprints: a customer's name turned into a code no one can turn back (F10 slice 2b, ADR-023).

The customer-data check needs to know our customers' names without Yaqzan
keeping them. Each name is turned into a keyed fingerprint (HMAC-SHA256)
with a key that lives in a key file on the machine, never in the database.
The database holds only fingerprints: without the key, not even a list of
every common name can say whose they are.

The key file has the master key format (``yaqzan-master-v1 <id> <base64>``);
its id is kept beside each fingerprint, so prints made with an older key are
never mistaken for current ones.
"""

from __future__ import annotations

import hashlib
import hmac

from yaqzan.core.vault.keys import MasterKey

PRINT_BYTES = 32
_PURPOSE = b"yaqzan name prints v1"  # a key of its own, even if a master key were ever reused here


class NamePrinter:
    """Turns text into its fingerprint. The key never appears in repr/str."""

    __slots__ = ("_key", "key_id")

    def __init__(self, key: MasterKey) -> None:
        self.key_id = key.key_id
        self._key = hmac.digest(key.material(), _PURPOSE, hashlib.sha256)

    def __call__(self, text: str) -> bytes:
        """The fingerprint of ``text`` (already made plain by the caller: lower case, no accents)."""
        return hmac.digest(self._key, text.encode("utf-8"), hashlib.sha256)

    def __repr__(self) -> str:
        return f"NamePrinter(key_id={self.key_id!r}, key=<hidden>)"

    __str__ = __repr__

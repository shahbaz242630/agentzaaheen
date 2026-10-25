"""Master keys: the keys that lock the per-credential data keys.

A master key lives in a root-only file outside the database and outside
backups (SECURITY §5.1). It is never read from an environment variable, never
printed, and its ``repr`` shows only its id.

Key file format (one line, ASCII)::

    agentzaaheen-master-v1 <key_id> <url-safe base64 of 32 random bytes>
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import stat
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

from agentzaaheen.core.vault.errors import VaultError, VaultErrorCode

KEY_BYTES = 32
_HEADER = "agentzaaheen-master-v1"
_KEY_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class MasterKey:
    """A 256-bit master key with an id. The key bytes never appear in repr/str."""

    __slots__ = ("_material", "key_id")

    def __init__(self, key_id: str, material: bytes) -> None:
        if not _KEY_ID.match(key_id):
            raise VaultError(VaultErrorCode.BAD_KEY_FILE, "key id must be lowercase letters, digits, '-'")
        if len(material) != KEY_BYTES:
            raise VaultError(VaultErrorCode.BAD_KEY_FILE, f"master key must be {KEY_BYTES} bytes")
        self.key_id = key_id
        self._material = bytes(material)

    @classmethod
    def generate(cls, key_id: str) -> MasterKey:
        """Create a new random master key (from the OS's secure random source)."""
        return cls(key_id, os.urandom(KEY_BYTES))

    def material(self) -> bytes:
        """Return the raw key bytes. Only the vault's cipher code calls this."""
        return self._material

    def to_file_text(self) -> str:
        """Serialise for writing to a root-only key file."""
        encoded = base64.urlsafe_b64encode(self._material).decode("ascii")
        return f"{_HEADER} {self.key_id} {encoded}\n"

    def __repr__(self) -> str:
        return f"MasterKey(key_id={self.key_id!r}, material=<hidden>)"

    __str__ = __repr__


def parse_key_text(text: str) -> MasterKey:
    """Parse the one-line key file format. Errors never echo the file content."""
    parts = text.strip().split(" ")
    if len(parts) != 3 or parts[0] != _HEADER:
        raise VaultError(VaultErrorCode.BAD_KEY_FILE, "unrecognised key file format")
    try:
        material = base64.urlsafe_b64decode(parts[2].encode("ascii"))
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise VaultError(VaultErrorCode.BAD_KEY_FILE, "key is not valid base64") from exc
    return MasterKey(parts[1], material)


def check_private_mode(mode: int, *, posix: bool, owner_uid: int = 0, my_uid: int = 0) -> None:
    """Refuse a key file that is unsafe to trust (POSIX only).

    Unsafe means: not a plain file, owned by another user, or that
    group or others can read or write.

    The server is Linux. On Windows (laptop staging, fake keys only) mode bits and
    owners mean nothing, so this check does not apply there.
    """
    if not posix:
        return
    if not stat.S_ISREG(mode):
        raise VaultError(VaultErrorCode.BAD_KEY_FILE, "key file must be a regular file")
    if owner_uid != my_uid:
        raise VaultError(VaultErrorCode.KEY_FILE_TOO_OPEN, "key file must be owned by the vault's user")
    if stat.S_IMODE(mode) & 0o077:
        raise VaultError(VaultErrorCode.KEY_FILE_TOO_OPEN, "key file must be readable by its owner only")


def _effective_uid(fallback: int) -> int:
    if sys.platform == "win32":
        return fallback
    return os.geteuid()


def load_master_key(path: Path) -> MasterKey:
    """Load a master key from a root-only file."""
    posix = os.name == "posix"
    # O_NOFOLLOW refuses a symlink; the checks then run on the very file we read.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        with os.fdopen(os.open(path, flags), "rb") as handle:
            info = os.fstat(handle.fileno())
            my_uid = _effective_uid(fallback=info.st_uid)
            check_private_mode(info.st_mode, posix=posix, owner_uid=info.st_uid, my_uid=my_uid)
            text = handle.read(1024).decode("ascii")
    except OSError as exc:
        raise VaultError(VaultErrorCode.BAD_KEY_FILE, f"cannot read key file {path.name}") from exc
    except UnicodeDecodeError as exc:
        raise VaultError(VaultErrorCode.BAD_KEY_FILE, "unrecognised key file format") from exc
    return parse_key_text(text)


class Keyring:
    """All master keys we can unwrap with, and the one we wrap new secrets with.

    Old keys stay in the ring after a rotation until every secret has been
    re-wrapped with the current one (see :func:`agentzaaheen.core.vault.rewrap`).
    """

    __slots__ = ("_keys", "current_id")

    def __init__(self, keys: Iterable[MasterKey], current_id: str) -> None:
        by_id: dict[str, MasterKey] = {}
        for key in keys:
            if key.key_id in by_id:
                raise VaultError(VaultErrorCode.BAD_KEY_FILE, f"duplicate master key id {key.key_id!r}")
            by_id[key.key_id] = key
        if current_id not in by_id:
            raise VaultError(VaultErrorCode.UNKNOWN_KEY, f"current key {current_id!r} is not in the keyring")
        self._keys: Mapping[str, MasterKey] = by_id
        self.current_id = current_id

    @property
    def current(self) -> MasterKey:
        """The key that wraps every newly sealed secret."""
        return self._keys[self.current_id]

    def get(self, key_id: str) -> MasterKey:
        """Return the key with ``key_id`` or fail closed."""
        key = self._keys.get(key_id)
        if key is None:
            raise VaultError(VaultErrorCode.UNKNOWN_KEY, f"master key {key_id!r} is not in the keyring")
        return key

    def __repr__(self) -> str:
        return f"Keyring(ids={sorted(self._keys)!r}, current={self.current_id!r})"

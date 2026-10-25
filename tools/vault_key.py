"""The vault's master key on this laptop: make it once, and find the Claude key's sealed slot.

Commands:
    make-key        create the master key file (once; refuses to overwrite)

The master key file unlocks every key sealed in the laptop database. It lives
outside the repository and outside the database (``KEY_FILE``). **Keep one
copy off the laptop** (the Chairman's choice, 25 Oct 2026: his OneDrive
Personal Vault): without it, a lost laptop means pasting every key again.
Never put it anywhere else, and never next to a copy of the database.

Usage:
    uv run python -m tools.vault_key make-key
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from agentzaaheen.core.broker import SealedSlot
from agentzaaheen.core.policy.claude_key import CONNECTOR, NAME
from agentzaaheen.core.store import Database
from agentzaaheen.core.vault import CredentialSlot, Keyring, VaultError, load_master_key
from tools.laptop_db import LAPTOP_TENANT, LAPTOP_WORKSPACES
from tools.name_prints import make_key

KEY_ID = "vault-1"
KEY_FILE = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Agent Zaaheen" / "keys" / "vault.key"
# The Claude key is shared by every project, so it lives in Agent Zaaheen's own workspace.
CLAUDE_WORKSPACE = next(w.workspace_id for w in LAPTOP_WORKSPACES if w.slug == "agentzaaheen")


def load_keyring(path: Path = KEY_FILE) -> Keyring | None:
    """The vault's keyring from the master key file; ``None`` if none is usable (after saying why)."""
    if not path.is_file():
        print(f"vault: no master key at {path}. Make one: python -m tools.vault_key make-key")
        return None
    try:
        key = load_master_key(path)
    except VaultError as exc:
        print(f"vault: the master key at {path} could not be used ({exc.code}).")
        return None
    return Keyring([key], current_id=key.key_id)


def claude_slot(db: Database, keyring: Keyring | None) -> SealedSlot:
    """The Claude key's sealed slot in the Agent Zaaheen workspace."""
    slot = CredentialSlot(
        tenant_id=LAPTOP_TENANT,
        project_id=CLAUDE_WORKSPACE,
        connector=CONNECTOR,
        name=NAME,
        environment="production",
    )
    # It can spend money, so it is not a read-only key.
    return SealedSlot(db.scoped(LAPTOP_TENANT, CLAUDE_WORKSPACE), keyring, slot, access="read_write")


def main(argv: Sequence[str] | None = None, *, key_file: Path = KEY_FILE) -> int:
    """Run one command; 0 when it worked."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("make-key")
    parser.parse_args(argv)
    if make_key(key_file, KEY_ID):
        print(f"vault: made the master key at {key_file}.")
        print("  Copy this file to your OneDrive Personal Vault now; keep no other copy.")
    else:
        print(f"vault: a master key is already at {key_file}; left it as it is.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

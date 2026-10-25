"""Our customers' names as fingerprints: make the key once, then read the names and save only the codes.

Commands:
    make-key                  create the fingerprint key file (once; refuses to overwrite)
    paddle --workspace SLUG --key-env ENV
                              read the customer names from that workspace's live Paddle,
                              turn each into a fingerprint at once, save only the fingerprints

The names are held in memory only while they are turned into fingerprints,
and are never printed, logged or stored. The tool prints counts only.

The key file lives outside the repository (``KEY_FILE``) and outside the
database. Losing it loses nothing: make a new key and read the names again.
Anyone with the key file and the database could test whether a guessed name
is a customer, so the key file is never copied anywhere else.

The Paddle key comes from the environment variable you name, like the
dashboard's (never printed). The fingerprints go into the laptop database,
which must be running (``tools.laptop_db up``).

Usage:
    uv run python -m tools.name_prints make-key
    uv run python -m tools.name_prints paddle --workspace zaaheen --key-env PADDLE_LIVE_API_KEY_ZAAHEEN
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import SecretStr

from agentzaaheen.core.llm_gateway import prints_of
from agentzaaheen.core.net import JsonClient, NetPolicy
from agentzaaheen.core.sdk import SourceError, explain
from agentzaaheen.core.store import Database, DatabaseSettings, NamePrints
from agentzaaheen.core.vault import MasterKey, NamePrinter, VaultError, load_master_key
from agentzaaheen.plugins.connectors.paddle import (
    ALLOWED_HOSTS,
    API_VERSION_HEADER,
    LIVE_BASE_URL,
    PaddleSource,
)
from tools.laptop_db import LAPTOP_TENANT, LAPTOP_WORKSPACES, prepare

KEY_ID = "names-1"
KEY_FILE = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Agent Zaaheen" / "keys" / "names.key"


@dataclass(frozen=True)
class Saved:
    """What one read did: names seen, fingerprints made, and how many of those were new."""

    names: int
    prints: int
    new: int


def make_key(path: Path = KEY_FILE) -> bool:
    """Create the key file; ``False`` (and nothing changed) if one is already there."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="ascii") as handle:  # "x": never overwrite a key
            handle.write(MasterKey.generate(KEY_ID).to_file_text())
    except FileExistsError:
        return False
    if os.name == "posix":
        path.chmod(0o600)
    return True


async def save(names: Sequence[str], rows: NamePrints, printer: NamePrinter, now: datetime) -> Saved:
    """Fingerprint ``names`` and save only the fingerprints."""
    prints = prints_of(names, printer)
    new = await rows.add(((p.kind.value, p.print) for p in prints), key_id=printer.key_id, at=now)
    return Saved(len(names), len(prints), new)


async def read_paddle_names(key: SecretStr, transport: httpx.AsyncBaseTransport | None = None) -> list[str]:
    """Every customer name in the live Paddle account (held in memory only)."""
    async with JsonClient(
        LIVE_BASE_URL,
        policy=NetPolicy(allowed_hosts=ALLOWED_HOSTS),
        headers=API_VERSION_HEADER,
        secret_headers={"Authorization": SecretStr(f"Bearer {key.get_secret_value()}")},
        transport=transport,
    ) as http:
        return await PaddleSource(http).customer_names()


async def paddle(  # noqa: PLR0913  # where from, where to, and the key
    workspace: UUID,
    *,
    key: SecretStr,
    printer: NamePrinter,
    url: SecretStr,
    now: datetime,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Saved:
    """Read the names, fingerprint them, save the fingerprints in ``workspace``."""
    names = await read_paddle_names(key, transport)
    db = await Database.open(DatabaseSettings(dsn=url, min_size=1, max_size=1))
    try:
        return await save(names, NamePrints(db.scoped(LAPTOP_TENANT, workspace)), printer, now)
    finally:
        await db.close()


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    key_file: Path = KEY_FILE,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Run one command; 0 when it worked."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("make-key")
    read = sub.add_parser("paddle")
    read.add_argument("--workspace", required=True, choices=[w.slug for w in LAPTOP_WORKSPACES])
    read.add_argument("--key-env", required=True, help="name of the env var holding the live Paddle key")
    ns = parser.parse_args(argv)
    if ns.command == "make-key":
        if make_key(key_file):
            print(f"name-prints: made the fingerprint key at {key_file}. Never copy it anywhere else.")
        else:
            print(f"name-prints: a key is already at {key_file}; left it as it is.")
        return 0
    try:
        printer = NamePrinter(load_master_key(key_file))
    except VaultError:
        print(f"name-prints: no usable key at {key_file}. Make one: python -m tools.name_prints make-key")
        return 1
    raw = (os.environ if environ is None else environ).get(ns.key_env, "").strip()
    if not raw:
        print(f"name-prints: {ns.key_env} is not set (could not run, not a pass).")
        return 1
    url = asyncio.run(prepare())
    if url is None:
        print("name-prints: the laptop database is not running. Start it: python -m tools.laptop_db up")
        return 1
    workspace = next(w.workspace_id for w in LAPTOP_WORKSPACES if w.slug == ns.workspace)
    try:
        saved = asyncio.run(
            paddle(
                workspace,
                key=SecretStr(raw),
                printer=printer,
                url=url,
                now=datetime.now(UTC),
                transport=transport,
            )
        )
    except SourceError as exc:
        said = explain(exc.reason_code, vendor="paddle")
        print(f"name-prints: reading Paddle failed with {exc.reason_code}.")
        print(f"  What: {said.what}\n  Why: {said.why}\n  What to do: {said.fix}")
        return 1
    print(
        f"name-prints: {ns.workspace}: {saved.names} customer names read, {saved.prints} fingerprints, "
        f"{saved.new} new. No name was kept."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

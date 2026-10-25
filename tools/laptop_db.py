"""The laptop database: a small Postgres in Docker that keeps its data, until the server exists (ADR-030).

The dev database (``tools.dev_db``) is throwaway: its data lives in memory and
is gone when it stops. The dashboard's registry switches must be remembered,
so they live here instead, on a Docker volume.

Commands:
    up        start it (creates it the first time; re-uses it after that)
    stop      stop it; its data stays on the volume for next time
    status    say whether it is running

Safety, the same as the dev database:
    - It only ever touches the one container named ``agentzaaheen-laptop-db`` that
      carries our label, and its own volume ``agentzaaheen-laptop-data``.
    - It refuses to start when Docker has less than ``dev_db.MIN_FREE_MB`` free,
      and is capped at ``dev_db.MEMORY_LIMIT_MB``.
    - It listens on 127.0.0.1 only, with a random password, a read-only root
      filesystem, no Linux capabilities and a non-root user.

:func:`prepare` (used by ``tools.dashboard --switches``) sets up the
``agentzaaheen`` database (:func:`set_up`): an owner role that applies the migrations, and a login
role for the dashboard that is a member of the least-privilege ``agentzaaheen_app``
role. Their passwords are derived from the container's own password, so
nothing new is stored anywhere. It also makes sure the laptop's one tenant
and its five workspaces exist, one per project (ADR-039): :data:`LAPTOP_TENANT`
and :data:`LAPTOP_WORKSPACES`. Their ids are derived from fixed names, so they
are the same on every run. Each of the five reads its GitHub repository
(:data:`LAPTOP_REPOS`), saved once, for a workspace that is active and has none yet.
"""

from __future__ import annotations

import argparse
import hashlib
import secrets
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID, uuid5

import asyncpg
from pydantic import SecretStr

from agentzaaheen.core.store import (
    Database,
    DatabaseSettings,
    WorkspaceRow,
    WorkspaceRows,
    WorkspaceSources,
    migrate,
)
from tools import dev_db

NAME = "agentzaaheen-laptop-db"
LABEL_VALUE = "laptop-db"
VOLUME = "agentzaaheen-laptop-data"
PORT = 55433
DATABASE = "agentzaaheen"
OWNER = "agentzaaheen_owner"
APP = "agentzaaheen_dashboard"
EXIT_OK = dev_db.EXIT_OK
EXIT_ERROR = dev_db.EXIT_ERROR
EXIT_LOW_MEMORY = dev_db.EXIT_LOW_MEMORY

_NAMESPACE = UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # the URL namespace
# The ids are made from fixed text that keeps the old name for good: change it and every id changes,
# and the saved switches, passkey and workspaces would belong to a tenant no one uses.
LAPTOP_TENANT = uuid5(_NAMESPACE, "yaqzan:laptop")
LAPTOP_WORKSPACES = tuple(
    WorkspaceRow(workspace_id=uuid5(_NAMESPACE, f"yaqzan:laptop:workspace:{slug}"), slug=slug, name=name)
    for slug, name in (
        ("zaaheen", "Zaaheen"),
        ("sandoq-kin", "Sandoq Kin"),
        ("agent-x", "Agent X"),
        ("eva", "Eva"),
        ("family-checkin", "Family Check-in"),
    )
)
# Which repository each project reads (checked against the local clones, 20 Oct 2026).
LAPTOP_REPOS: dict[str, str] = {
    "zaaheen": "shahbaz242630/zaaheen",
    "sandoq-kin": "shahbaz242630/Document-Vault",
    "agent-x": "shahbaz242630/agent-x",
    "eva": "shahbaz242630/AI-Agent-SAAS",
    "family-checkin": "shahbaz242630/Family-Checkin",
}


def _ours(run: dev_db.Runner) -> str:
    return dev_db.state(run, name=NAME, label_value=LABEL_VALUE)


def _run_args(password: str) -> list[str]:
    uid = "999"  # the postgres user inside the official image
    return [
        "run", "--detach", "--name", NAME, "--label", f"{dev_db.LABEL_KEY}={LABEL_VALUE}",
        "--memory", f"{dev_db.MEMORY_LIMIT_MB}m", "--memory-swap", f"{dev_db.MEMORY_LIMIT_MB}m",
        "--cpus", "1",
        "--pids-limit", "200", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", f"{uid}:{uid}",
        "--volume", f"{VOLUME}:/var/lib/postgresql/data",
        "--tmpfs", f"/var/run/postgresql:rw,uid={uid},gid={uid},size=8m",
        "--tmpfs", "/tmp:rw,size=64m",  # noqa: S108  # nosec B108  # inside the container, not on this machine
        "--env", f"POSTGRES_PASSWORD={password}",
        "--publish", f"{dev_db.HOST}:{PORT}:5432",
        dev_db.IMAGE,
        "-c", "shared_buffers=64MB", "-c", "max_connections=50",
    ]  # fmt: skip


def up(run: dev_db.Runner = dev_db.run_docker, sleep: dev_db.Sleeper = time.sleep) -> int:
    """Start the laptop database (creating it the first time). Returns an exit code."""
    current = _ours(run)
    if current == "foreign":
        print(f"laptop-db: a container named {NAME} exists but is not ours. Leaving it alone.")
        return EXIT_ERROR
    if current == "running":
        print("laptop-db: already running.")
        return EXIT_OK
    if not dev_db.enough_memory(run):
        return EXIT_LOW_MEMORY
    # A stopped container keeps its password, which the data on the volume was set up with.
    args = ["start", NAME] if current == "stopped" else _run_args(secrets.token_urlsafe(24))
    code, _ = run(args)
    if code != 0 or not dev_db.wait_ready(run, sleep, name=NAME):
        print("laptop-db: failed to start.")
        return EXIT_ERROR
    print(f"laptop-db: running on {dev_db.HOST}:{PORT} (data kept on the Docker volume {VOLUME}).")
    return EXIT_OK


def stop(run: dev_db.Runner = dev_db.run_docker) -> int:
    """Stop the laptop database; its data stays for next time."""
    current = _ours(run)
    if current == "foreign":
        print(f"laptop-db: a container named {NAME} exists but is not ours. Leaving it alone.")
        return EXIT_ERROR
    if current != "running":
        print("laptop-db: not running.")
        return EXIT_OK
    code, _ = run(["stop", NAME])
    return EXIT_OK if code == 0 else EXIT_ERROR


def derived_password(admin_password: str, role: str) -> str:
    """A role's password, derived from the container's own (hex, so it is safe inside DDL)."""
    return hashlib.blake2b(role.encode(), key=admin_password.encode()[:64], digest_size=24).hexdigest()


# The product was called Yaqzan until 25 Oct 2026: a laptop database made then has these names.
_OLD_ROLES = {"yaqzan_owner": OWNER, "yaqzan_dashboard": APP, "yaqzan_app": "agentzaaheen_app"}
_OLD_DATABASE = "yaqzan"


async def adopt_old_names(conn: asyncpg.Connection[asyncpg.Record], database: str = DATABASE) -> None:
    """Rename the old roles and database to today's names, once (the schema is renamed by ``migrate``)."""
    for old, new in _OLD_ROLES.items():
        names = await conn.fetch("SELECT rolname FROM pg_roles WHERE rolname = ANY($1::text[])", [old, new])
        if {r["rolname"] for r in names} == {old}:
            await conn.execute(f"ALTER ROLE {old} RENAME TO {new}")  # constants, never user input
    found = await conn.fetch(
        "SELECT datname FROM pg_database WHERE datname = ANY($1::text[])", [_OLD_DATABASE, database]
    )
    if {r["datname"] for r in found} == {_OLD_DATABASE}:
        await conn.execute(f'ALTER DATABASE "{_OLD_DATABASE}" RENAME TO "{database}"')


async def _ensure_role(
    conn: asyncpg.Connection[asyncpg.Record], name: str, options: str, password: str
) -> None:
    exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", name)
    verb = "ALTER" if exists else "CREATE"
    # Role names are constants and the password is hex derived here, never user input.
    await conn.execute(f"{verb} ROLE {name} {options} PASSWORD '{password}'")


def _url(admin: str, user: str, password: str, database: str) -> str:
    parts = urlsplit(admin)
    return f"postgresql://{user}:{password}@{parts.hostname}:{parts.port}/{database}"


async def prepare(run: dev_db.Runner = dev_db.run_docker) -> SecretStr | None:
    """The dashboard's connection URL to the laptop database, set up first; ``None`` if it is not running."""
    admin = dev_db.admin_url(run, name=NAME, label_value=LABEL_VALUE, port=PORT)
    return None if admin is None else await set_up(admin)


async def set_up(admin: str, database: str = DATABASE) -> SecretStr:
    """Set up the roles, ``database``, migrations and workspaces if missing; return the dashboard's URL.

    Safe to run every time: roles get their (derived) passwords again, and
    migrations and workspaces already there are skipped.
    """
    admin_password = urlsplit(admin).password or ""
    owner_password = derived_password(admin_password, OWNER)
    app_password = derived_password(admin_password, APP)
    conn = await asyncpg.connect(admin)
    try:
        await adopt_old_names(conn, database)
        await _ensure_role(conn, OWNER, "LOGIN CREATEROLE NOSUPERUSER", owner_password)
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", database):
            await conn.execute(f'CREATE DATABASE "{database}" OWNER {OWNER}')
    finally:
        await conn.close()
    owner = await Database.open(DatabaseSettings(dsn=SecretStr(_url(admin, OWNER, owner_password, database))))
    try:
        await migrate(owner)
    finally:
        await owner.close()
    conn = await asyncpg.connect(admin)
    try:
        await _ensure_role(conn, APP, "LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB", app_password)
        await conn.execute(f"GRANT agentzaaheen_app TO {APP}")
    finally:
        await conn.close()
    url = SecretStr(_url(admin, APP, app_password, database))
    app = await Database.open(DatabaseSettings(dsn=url))
    try:
        workspaces = WorkspaceRows(app.scoped(LAPTOP_TENANT))
        await workspaces.add_tenant("Agent Zaaheen")
        for workspace in LAPTOP_WORKSPACES:
            await workspaces.add(workspace)
        await _connect_repos(app, await workspaces.workspaces())
    finally:
        await app.close()
    return url


async def _connect_repos(app: Database, active: Sequence[WorkspaceRow]) -> None:
    """Save each laptop project's repository once (an archived or removed one is left alone)."""
    for workspace in active:
        repo = LAPTOP_REPOS.get(workspace.slug)
        if repo is None or workspace not in LAPTOP_WORKSPACES:
            continue
        sources = WorkspaceSources(app.scoped(LAPTOP_TENANT, workspace.workspace_id))
        if await sources.github() is None:
            await sources.connect_github(repo, by="system:laptop-setup", at=datetime.now(UTC))


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(prog="laptop_db", description="The laptop database (keeps its data).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("up")
    sub.add_parser("stop")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    if args.command == "up":
        return up()
    if args.command == "stop":
        return stop()
    print(f"laptop-db: {_ours(dev_db.run_docker)}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

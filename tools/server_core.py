"""Agent Zaaheen on our server (F17 slice 7): the dashboard program, run by systemd, on 127.0.0.1 only.

The server is on all day, so the 05:00 summaries and the 07:00 briefing can be
made there instead of on the laptop. This starts the same program the laptop
runs (``tools.dashboard --switches``), with three differences:

- **Secrets come from systemd credentials**, never from files we write or
  from the environment: systemd decrypts each one into ``$CREDENTIALS_DIRECTORY``
  for this service only. ``postgres_password`` opens the server's Postgres,
  ``vault_key`` is the server's own vault master key, and each credential
  named in capitals (``PADDLE_LIVE_API_KEY_ZAAHEEN``, ``SUPABASE_READ_TOKEN_*``
  …) is handed to the dashboard under that name, as the laptop's env vars are.
- **The database** is the server's Postgres on 127.0.0.1, set up the same way
  as the laptop's (roles, migrations, the five workspaces).
- **Nothing reaches the phone unless ``--send`` is given.** The laptop sends
  the alerts and the briefing until the server has proved itself; two senders
  would mean two of everything. With ``--send`` the ntfy topic and the
  Healthchecks briefing ping are used, if their credentials are there.

There is no web page here yet: a one-line placeholder (written at start, since
HTML files are never committed) is served instead of the built UI, so Node is
not needed on the server.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from urllib.parse import quote

from pydantic import SecretStr

from tools import dashboard, laptop_db

CREDENTIALS = "CREDENTIALS_DIRECTORY"
DB_PASSWORD = "postgres_password"  # noqa: S105  # nosec B105  # a credential's name, not a secret
VAULT_KEY = "vault_key"
DB_HOST = "127.0.0.1"
DB_PORT = 5432
CONFIG = Path("/opt/agentzaaheen/config")
LOG_DIR = Path("/var/log/agentzaaheen")
PAGE = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Agent Zaaheen server</title></head>'
    "<body><p>Agent Zaaheen is running on the server: it makes the daily summaries and the morning"
    " briefing. There is no dashboard page here yet; use the laptop's.</p></body></html>\n"
)
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
# Which credential turns on which reader; the Supabase tokens are named inside supabase.yaml.
READERS = {
    "PADDLE_LIVE_API_KEY_ZAAHEEN": "--paddle-key-env",
    "GITHUB_READ_TOKEN_AGENTZAAHEEN": "--github-token-env",
}
SENDERS = {
    "AGENTZAAHEEN_NTFY_TOPIC": "--ntfy-topic-env",
    "AGENTZAAHEEN_BRIEFING_PING_URL": "--briefing-ping-env",
}


class NotReadyError(Exception):
    """Something the service needs is missing; the message says what, never a secret."""


def credentials(folder: Path) -> dict[str, str]:
    """Every credential systemd handed us, by name (secrets stay in memory, never printed)."""
    if not folder.is_dir():
        raise NotReadyError(f"no credentials folder at {folder}; run this only as the systemd service")
    return {f.name: f.read_text(encoding="utf-8").strip() for f in sorted(folder.iterdir()) if f.is_file()}


def admin_url(password: str) -> str:
    """The server Postgres's admin address (the password quoted, so any character is safe)."""
    return f"postgresql://postgres:{quote(password, safe='')}@{DB_HOST}:{DB_PORT}/postgres"


def plan(
    creds: Mapping[str, str], folder: Path, config: Path, *, send: bool
) -> tuple[list[str], dict[str, str]]:
    """The dashboard's options and the environment it reads its keys from."""
    for needed in (DB_PASSWORD, VAULT_KEY):
        if not creds.get(needed):
            raise NotReadyError(f"the credential {needed} is missing")
    env = {name: value for name, value in creds.items() if ENV_NAME.match(name) and value}
    argv = ["--switches", "--vault-key", str(folder / VAULT_KEY), "--log-dir", str(LOG_DIR)]
    for name, option in READERS.items():
        if name in env:
            argv += [option, name]
    for option, file in (("--uptime", "uptime.yaml"), ("--supabase", "supabase.yaml")):
        if (config / file).is_file():
            argv += [option, str(config / file)]
    if send:
        for name, option in SENDERS.items():
            if name in env:
                argv += [option, name]
    return argv, env


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    start: Callable[..., int] = dashboard.main,
) -> int:
    """Start Agent Zaaheen on the server; 1 (after saying why) if something it needs is missing."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--send", action="store_true", help="send alerts and the briefing to the phone")
    parser.add_argument(
        "--config", type=Path, default=CONFIG, help="the folder with uptime.yaml and supabase.yaml"
    )
    ns = parser.parse_args(argv)
    outer = os.environ if environ is None else environ
    try:
        if not outer.get(CREDENTIALS):
            raise NotReadyError("no credentials from systemd; run this only as the systemd service")
        folder = Path(outer[CREDENTIALS])
        creds = credentials(folder)
        options, env = plan(creds, folder, ns.config, send=ns.send)
    except NotReadyError as exc:
        print(f"server: not started: {exc}.")
        return 1
    password = creds[DB_PASSWORD]

    async def prepare() -> SecretStr | None:
        return await laptop_db.set_up(admin_url(password))

    print(f"server: starting with {len(env)} keys; phone {'on' if ns.send else 'off (the laptop sends)'}.")
    with tempfile.TemporaryDirectory(prefix="agentzaaheen-page-") as page:
        (Path(page) / "index.html").write_text(PAGE, encoding="utf-8")
        result: int = start(options, environ=env, static_dir=Path(page), prepare=prepare)
    return result


if __name__ == "__main__":
    sys.exit(main())

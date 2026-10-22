"""A throwaway Postgres in Docker for local development and CI. Never production.

Commands:
    up [--wait-minutes N]   start it (re-uses a running one); with --wait, retry
                            every 5 minutes while Docker is short of memory
    down                    remove it (its data lives in memory and is gone)
    status                  say whether it is running

Safety, because other projects run containers on the same Docker:
    - It only ever touches the one container named ``yaqzan-dev-db`` that also
      carries our label. Anything else with that name is left alone.
    - It refuses to start when Docker has less than ``MIN_FREE_MB`` free, and is
      capped at ``MEMORY_LIMIT_MB`` so it can never starve another project.
    - It listens on 127.0.0.1 only, with a random password, a read-only root
      filesystem, no Linux capabilities and a non-root user.
"""

from __future__ import annotations

import argparse
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Sequence

NAME = "yaqzan-dev-db"
LABEL_KEY = "com.yaqzan.role"
LABEL_VALUE = "dev-db"
IMAGE = "pgvector/pgvector:0.8.7-pg17@sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d"
HOST = "127.0.0.1"
PORT = 55432
MEMORY_LIMIT_MB = 512
MIN_FREE_MB = 1024
RETRY_SECONDS = 300
READY_TIMEOUT_S = 60

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_LOW_MEMORY = 3

Runner = Callable[[Sequence[str]], tuple[int, str]]
Sleeper = Callable[[float], None]

_UNITS = {"b": 1, "kib": 1024, "mib": 1024**2, "gib": 1024**3, "kb": 1000, "mb": 1000**2, "gb": 1000**3}
_SIZE = re.compile(r"^\s*([0-9.]+)\s*([a-zA-Z]+)\s*$")


def run_docker(args: Sequence[str]) -> tuple[int, str]:
    """Run ``docker <args>`` without a shell; return (exit code, stdout)."""
    done = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607
    return done.returncode, done.stdout.strip()


def parse_size(text: str) -> int:
    """``'123.4MiB'`` → bytes."""
    match = _SIZE.match(text)
    if match is None or match.group(2).lower() not in _UNITS:
        raise ValueError(f"unrecognised size {text!r}")
    return int(float(match.group(1)) * _UNITS[match.group(2).lower()])


def free_mb(run: Runner) -> int | None:
    """Docker's memory minus what every running container uses. None if Docker can't say."""
    code, total = run(["info", "--format", "{{.MemTotal}}"])
    if code != 0 or not total.isdigit():
        return None
    code, usage = run(["stats", "--no-stream", "--format", "{{.MemUsage}}"])
    if code != 0:
        return None
    try:
        used = sum(parse_size(line.split("/")[0]) for line in usage.splitlines() if line.strip())
    except ValueError:
        return None
    return (int(total) - used) // 1024**2


def state(run: Runner, *, name: str = NAME, label_value: str = LABEL_VALUE) -> str:
    """``absent``, ``running``, ``stopped``, or ``foreign`` (same name, not ours: never touched)."""
    fmt = '{{index .Config.Labels "' + LABEL_KEY + '"}}|{{.State.Running}}'
    code, out = run(["inspect", "--type", "container", "--format", fmt, name])
    if code != 0:
        return "absent"
    label, _, running = out.partition("|")
    if label != label_value:
        return "foreign"
    return "running" if running == "true" else "stopped"


def _run_args(password: str) -> list[str]:
    uid = "999"  # the postgres user inside the official image
    return [
        "run", "--detach", "--name", NAME, "--label", f"{LABEL_KEY}={LABEL_VALUE}",
        "--memory", f"{MEMORY_LIMIT_MB}m", "--memory-swap", f"{MEMORY_LIMIT_MB}m", "--cpus", "1",
        "--pids-limit", "200", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", f"{uid}:{uid}",
        "--tmpfs", f"/var/lib/postgresql/data:rw,uid={uid},gid={uid},mode=0700,size=256m",
        "--tmpfs", f"/var/run/postgresql:rw,uid={uid},gid={uid},size=8m",
        "--tmpfs", "/tmp:rw,size=64m",  # noqa: S108  # nosec B108  # inside the container, not on this machine
        "--env", f"POSTGRES_PASSWORD={password}",
        "--publish", f"{HOST}:{PORT}:5432",
        IMAGE,
        # Throwaway data in memory: durability settings off for speed.
        "-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off",
        "-c", "shared_buffers=64MB", "-c", "max_connections=200",
    ]  # fmt: skip


def wait_ready(run: Runner, sleep: Sleeper, timeout_s: float = READY_TIMEOUT_S, *, name: str = NAME) -> bool:
    """Wait until Postgres accepts TCP connections (the init-time server listens on a socket only)."""
    waited = 0.0
    while waited < timeout_s:
        code, _ = run(["exec", name, "pg_isready", "-q", "-h", "127.0.0.1", "-U", "postgres"])
        if code == 0:
            return True
        sleep(1.0)
        waited += 1.0
    return False


def enough_memory(run: Runner) -> bool:
    """Whether Docker has ``MIN_FREE_MB`` free (saying why not). Unknown counts as not enough."""
    free = free_mb(run)
    if free is None:
        print("dev-db: could not read Docker memory (is Docker running?); not starting: unknown is unsafe.")
        return False
    if free < MIN_FREE_MB:
        print(
            f"dev-db: Docker has {free} MB free, need {MIN_FREE_MB} MB."
            " Not starting, to protect other projects."
        )
        return False
    return True


def up(run: Runner = run_docker, sleep: Sleeper = time.sleep, *, wait_minutes: int = 0) -> int:
    """Start the dev database, waiting for memory if asked. Returns an exit code."""
    current = state(run)
    if current == "foreign":
        print(f"dev-db: a container named {NAME} exists but is not ours. Leaving it alone.")
        return EXIT_ERROR
    if current == "running":
        print("dev-db: already running.")
        return EXIT_OK
    deadline = wait_minutes * 60
    waited = 0
    while not enough_memory(run):
        if waited >= deadline:
            return EXIT_LOW_MEMORY
        print(f"dev-db: checking again in {RETRY_SECONDS // 60} minutes.")
        sleep(RETRY_SECONDS)
        waited += RETRY_SECONDS
    args = ["start", NAME] if current == "stopped" else _run_args(secrets.token_urlsafe(24))
    code, _ = run(args)
    if code != 0 or not wait_ready(run, sleep):
        print("dev-db: failed to start.")
        return EXIT_ERROR
    print(f"dev-db: running on {HOST}:{PORT}.")
    return EXIT_OK


def down(run: Runner = run_docker) -> int:
    """Remove the dev database (only if it is ours)."""
    current = state(run)
    if current == "foreign":
        print(f"dev-db: a container named {NAME} exists but is not ours. Leaving it alone.")
        return EXIT_ERROR
    if current == "absent":
        print("dev-db: not running.")
        return EXIT_OK
    code, _ = run(["rm", "--force", NAME])
    return EXIT_OK if code == 0 else EXIT_ERROR


def admin_url(
    run: Runner = run_docker, *, name: str = NAME, label_value: str = LABEL_VALUE, port: int = PORT
) -> str | None:
    """Superuser URL for tests, read from our own running container. None if it is not running."""
    if state(run, name=name, label_value=label_value) != "running":
        return None
    code, env = run(["inspect", "--type", "container", "--format", "{{json .Config.Env}}", name])
    match = re.search(r'"POSTGRES_PASSWORD=([^"]+)"', env) if code == 0 else None
    if match is None:
        return None
    return f"postgresql://postgres:{match.group(1)}@{HOST}:{port}/postgres"


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(prog="dev_db", description="Throwaway Postgres for development and CI.")
    sub = parser.add_subparsers(dest="command", required=True)
    up_parser = sub.add_parser("up")
    up_parser.add_argument("--wait-minutes", type=int, default=0)
    sub.add_parser("down")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    if args.command == "up":
        return up(wait_minutes=args.wait_minutes)
    if args.command == "down":
        return down()
    print(f"dev-db: {state(run_docker)}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

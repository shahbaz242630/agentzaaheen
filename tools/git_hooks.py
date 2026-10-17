"""Local git hooks: fast checks before every commit and push.

Hooks can be skipped, so CI repeats every check (CI is the real gate).
Never use ``--no-verify``.

Install once per clone:
    uv run python -m tools.git_hooks install

pre-commit: path guard on staged files, secret scan of staged changes, ruff.
pre-push:   full test suite with coverage floors.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_HOOK_TEMPLATE = """#!/bin/sh
# Installed by tools/git_hooks.py. Do not edit; re-run the installer instead.
exec uv run python -m tools.git_hooks {stage}
"""


def _run(argv: Sequence[str]) -> int:
    print(f"$ {' '.join(argv)}")
    return subprocess.run(list(argv), cwd=ROOT, check=False).returncode  # noqa: S603


def pre_commit() -> int:
    """Block the commit if any staged file is not code, contains a secret, or fails lint."""
    gitleaks = shutil.which("gitleaks")
    if gitleaks is None:
        print("pre-commit: gitleaks is not installed. Install it (see CI workflow) before committing.")
        return 1
    steps: list[list[str]] = [
        [sys.executable, "-m", "tools.path_guard", "--staged"],
        [gitleaks, "git", "--pre-commit", "--staged", "--redact", "--no-banner"],
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "ruff", "format", "--check", "."],
    ]
    for step in steps:
        if _run(step) != 0:
            print("pre-commit: blocked. Fix the problem above and commit again.")
            return 1
    return 0


def pre_push() -> int:
    """Block the push if the tests or coverage floors fail."""
    for step in ([sys.executable, "-m", "pytest", "-q"], [sys.executable, "-m", "tools.coverage_gate"]):
        if _run(step) != 0:
            print("pre-push: blocked. Fix the problem above and push again.")
            return 1
    return 0


def install() -> int:
    """Write the pre-commit and pre-push hooks into this clone's .git/hooks."""
    hooks_dir = ROOT / ".git" / "hooks"
    if not hooks_dir.is_dir():
        print("install: .git/hooks not found. Run this from inside the repository clone.")
        return 1
    for stage in ("pre-commit", "pre-push"):
        hook = hooks_dir / stage
        hook.write_text(_HOOK_TEMPLATE.format(stage=stage), encoding="utf-8", newline="\n")
        hook.chmod(0o755)
        print(f"install: wrote {hook}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch to ``install``, ``pre-commit`` or ``pre-push``."""
    args = list(sys.argv[1:] if argv is None else argv)
    commands = {"install": install, "pre-commit": pre_commit, "pre-push": pre_push}
    if len(args) != 1 or args[0] not in commands:
        print(f"usage: python -m tools.git_hooks {{{','.join(commands)}}}")
        return 2
    return commands[args[0]]()


if __name__ == "__main__":
    sys.exit(main())

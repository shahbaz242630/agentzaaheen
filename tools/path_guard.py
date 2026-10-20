"""Path guard: allow only code files into the repository (ADR-014).

Yaqzan's repository is public and holds code only. Documentation, plans,
handoffs, research, runbooks, project configs and secrets live outside the
repo. ``.gitignore`` is the first fence; this script is the second (run as a
pre-commit hook on staged files) and third (run in CI on every tracked file).

It is an ALLOWLIST: a path passes only if it matches one of ``ALLOWED``.
Anything new (a README, a stray PDF, a config export) fails until someone
deliberately extends the list, which requires Chairman approval.

Usage:
    python -m tools.path_guard --staged     # pre-commit: files being committed
    python -m tools.path_guard --tracked    # CI: every file in the repo
    python -m tools.path_guard PATH [PATH]  # explicit paths (tests)
"""

from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
from collections.abc import Iterable, Sequence

ALLOWED: tuple[str, ...] = (
    # Python source, tests and tooling
    "src/yaqzan/*.py",
    "src/yaqzan/**/*.py",
    "src/yaqzan/py.typed",
    "tests/*.py",
    "tests/**/*.py",
    "tools/*.py",
    # Plugin content that IS product code (agent prompts, skill playbooks, manifests)
    "src/yaqzan/plugins/agents/*/prompt.md",
    "src/yaqzan/plugins/skills/*/SKILL.md",
    "src/yaqzan/plugins/*/*/plugin.yaml",
    # The dashboard web UI (F04): source, its config, and the API contract fixture
    "apps/dashboard/index.html",
    "apps/dashboard/package.json",
    "apps/dashboard/package-lock.json",
    "apps/dashboard/.npmrc",
    "apps/dashboard/tsconfig.json",
    "apps/dashboard/vite.config.ts",
    "apps/dashboard/eslint.config.js",
    "apps/dashboard/public/*.svg",
    "apps/dashboard/src/**/*.ts",
    "apps/dashboard/src/**/*.tsx",
    "apps/dashboard/src/**/*.css",
    "apps/dashboard/src/contract/sample.json",
    # The external uptime checker (ADR-032): a Cloudflare Worker; its secrets are never files here
    "apps/checker/package.json",
    "apps/checker/package-lock.json",
    "apps/checker/.npmrc",
    "apps/checker/tsconfig.json",
    "apps/checker/eslint.config.js",
    "apps/checker/wrangler.jsonc",
    "apps/checker/schema.sql",
    "apps/checker/src/*.ts",
    # Project and tool configuration
    "pyproject.toml",
    "uv.lock",
    ".gitignore",
    ".gitattributes",
    ".python-version",
    ".pre-commit-config.yaml",
    ".gitleaks.toml",
    # GitHub automation
    ".github/workflows/*.yml",
    ".github/dependabot.yml",
    ".github/CODEOWNERS",
)

# Checked before the allowlist so an allowed pattern can never let these through.
DENIED: tuple[str, ...] = (
    "*.env",
    "*.env.*",
    ".env*",
    "*.pem",
    "*.key",
    "*.p8",
    "*.p12",
    "*.pfx",
)


def _matches(path: str, patterns: Iterable[str]) -> bool:
    name = path.rsplit("/", 1)[-1]
    for pattern in patterns:
        if fnmatch.fnmatchcase(path, pattern):
            return True
        if "/" not in pattern and fnmatch.fnmatchcase(name, pattern):
            return True
    return False


def _matches_allowed(path: str) -> bool:
    for pattern in ALLOWED:
        if "**/" in pattern:
            # "a/**/*.py": any depth under "a/". fnmatch's "*" crosses "/", so
            # dropping "**/" leaves a pattern that matches every depth.
            if fnmatch.fnmatchcase(path, pattern.replace("**/", "")):
                return True
        elif _single_level_match(path, pattern):
            return True
    return False


def _single_level_match(path: str, pattern: str) -> bool:
    """Match where each "*" stays inside one path segment."""
    path_parts = path.split("/")
    pattern_parts = pattern.split("/")
    if len(path_parts) != len(pattern_parts):
        return False
    return all(fnmatch.fnmatchcase(p, q) for p, q in zip(path_parts, pattern_parts, strict=True))


def check(paths: Sequence[str]) -> list[str]:
    """Return the paths that are NOT allowed in the repository."""
    rejected: list[str] = []
    for raw in paths:
        path = raw.replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        if not path:
            continue
        if _matches(path, DENIED) or not _matches_allowed(path):
            rejected.append(path)
    return rejected


def _git_paths(args: Sequence[str]) -> list[str]:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", *args],  # noqa: S607 - git resolved from PATH by design
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the guard; return 0 if every path is allowed, 1 otherwise."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true", help="check files staged for commit")
    group.add_argument("--tracked", action="store_true", help="check every tracked file")
    parser.add_argument("paths", nargs="*")
    ns = parser.parse_args(argv)

    if ns.staged:
        paths = _git_paths(["diff", "--cached", "--name-only", "--diff-filter=ACMR"])
    elif ns.tracked:
        paths = _git_paths(["ls-files"])
    else:
        paths = list(ns.paths)

    rejected = check(paths)
    if rejected:
        print("path-guard: these files are not allowed in the Yaqzan repo (code only, ADR-014):")
        for path in rejected:
            print(f"  - {path}")
        print("Docs, plans, configs and secrets belong outside the repository.")
        return 1
    print(f"path-guard: {len(paths)} file(s) checked, all allowed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""What the scout may read from a repository: secret files never, a short fixed choice of the rest."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from yaqzan.core.sdk import MAX_FILE_CHARS, MAX_TOTAL_CHARS, RepoBundle, RepoFile, is_secret_path, pick_files

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "path",
    [
        ".env", ".env.production", "apps/api/.env.local", "prod.env", "certs/site.pem", "deploy/server.key",
        "id_ed25519", "ios/cert.p12", "a/b/token.pfx", "github.token", "auth.json", "data/app.db",
        "local.sqlite3", "dump.bak", "db.backup", "pg.dump", "Vault.kdbx", "credentials.json", ".npmrc",
        ".git/config", "x/.ssh/known_hosts", ".aws/config", "secrets/anything.txt", "Keys/readme.md",
        "node_modules/react/index.js", "web/node_modules/a.js", ".venv/lib/x.py", ".ENV",
    ],
)  # fmt: skip
def test_secret_files_are_refused(path: str) -> None:
    assert is_secret_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "src/env.ts",
        "docs/keys-and-locks.md",
        "package.json",
        "environment.md",
        "src/keyboard.tsx",
    ],
)
def test_ordinary_files_are_not_secret(path: str) -> None:
    assert not is_secret_path(path)


def test_the_scout_reads_about_then_manifests_then_workflows_then_money_and_measuring() -> None:
    paths = [
        "src/lib/stripe.ts", "README.md", ".github/workflows/ci.yml", "apps/web/package.json",
        "docs/deep/a/package.json", "AGENTS.md", "src/analytics/posthog.ts", "src/pricing.png", ".env",
        "src/app.tsx", "package.json", "docs/README.md",
    ]  # fmt: skip
    assert pick_files(paths) == [
        "AGENTS.md",
        "README.md",
        "package.json",
        "apps/web/package.json",
        ".github/workflows/ci.yml",
        "src/analytics/posthog.ts",
        "src/lib/stripe.ts",
    ]


def test_at_most_twenty_files_and_never_a_secret() -> None:
    paths = [f"src/billing/f{i:02}.ts" for i in range(30)] + [f".github/workflows/w{i}.yml" for i in range(9)]
    paths += ["secrets/stripe.ts", "README.md", "..", "../stripe.ts"]
    picked = pick_files(paths)
    assert len(picked) == 11  # README, 4 workflows, 6 money files
    assert picked[0] == "README.md"
    assert not [p for p in picked if is_secret_path(p) or ".." in p]


def _bundle(**change: object) -> RepoBundle:
    fields: dict[str, object] = {
        "repo": "o/r",
        "branch": "main",
        "paths": ("README.md", "src/a.ts"),
        "total_files": 2,
        "secret_files": 1,
        "files": (RepoFile(path="README.md", text="# R", cut=False),),
        "read_at": NOW,
    }
    return RepoBundle.model_validate(fields | change)


def test_a_bundle_says_which_cited_paths_do_not_exist() -> None:
    assert _bundle().missing(["src/a.ts", "apps/yaqzan/plugins/x.py", "README.md"]) == (
        "apps/yaqzan/plugins/x.py",
    )


@pytest.mark.parametrize(
    "change",
    [
        {"paths": ("README.md", "README.md")},
        {"total_files": 1},
        {"files": (RepoFile(path="other.md", text="", cut=False),)},
        {"paths": ("README.md", ".env"), "total_files": 2},
        {"files": tuple(RepoFile(path="README.md", text="x" * MAX_FILE_CHARS, cut=True) for _ in range(5))},
        {"files": (RepoFile.model_construct(path="README.md", text="x" * (MAX_TOTAL_CHARS + 1), cut=False),)},
        {"read_at": datetime(2026, 10, 24, 9, 0)},  # noqa: DTZ001  # a naive time on purpose
    ],
)
def test_a_bundle_that_breaks_a_rule_is_refused(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _bundle(**change)


@pytest.mark.parametrize("path", ["/etc/passwd", "a/../b", "..", "a\\b", "a\x00b"])
def test_a_file_path_must_be_plain(path: str) -> None:
    with pytest.raises(ValidationError):
        RepoFile(path=path, text="", cut=False)

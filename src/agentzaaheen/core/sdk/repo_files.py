"""What a project's repository holds, for the scout to read (ADR-039, F17 slice 5a).

A code-hosting connector reads one repository and returns a
:class:`RepoBundle`: every file path it may show (secret files left out) and
the text of a short, fixed choice of files. All file text is written by
anyone who can push to the repository, so it is **untrusted**: it is passed
to a model only inside the untrusted tag, and never as a fact or an
instruction.

The rules here are the same for every connector (GitHub now, a local folder
through the Folder Bridge later, ADR-040):

- :func:`is_secret_path`: files that are never read or listed (keys,
  ``.env`` files, databases, backups, vendored folders).
- :func:`pick_files`: which files the scout reads, in order, within
  :data:`MAX_FILES`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from agentzaaheen.core.sdk.code import REPO_PATTERN

_BRANCH = r"^[A-Za-z0-9._/-]{1,255}$"  # as in RepoHealth
_PLAIN = re.compile(r"[^\x00-\x1f\x7f\\]{1,400}")

MAX_PATHS = 5000  # paths kept in a bundle; more are counted, not listed
MAX_FILES = 20  # files read
MAX_FILE_CHARS = 6000  # characters kept from one file
MAX_TOTAL_CHARS = 24000  # characters kept from all files (about 6k tokens; the path list is extra)
MAX_FILE_BYTES = 200_000  # larger files are not read at all

# ADR-040: secrets are refused by default, not just hidden. Matched against the file name.
_SECRET_NAMES = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "id_*", "*.token", "auth.json",
    "*.db", "*.sqlite", "*.sqlite3", "*.bak", "*.backup", "*.dump", "*.kdbx", "credentials*",
    ".npmrc", ".pypirc", ".netrc",
)  # fmt: skip
# ... and against every folder on the way to it.
_SECRET_FOLDERS = frozenset(
    {".git", ".ssh", ".aws", ".gnupg", "secrets", "keys", "certs", "node_modules", ".venv", "venv"}
)

# The order the scout reads in: what the project says it is, what it is built from, how it ships,
# then files whose names point to money or measuring (where the business roles are decided).
_ABOUT = ("README", "README.*", "AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md")
_MANIFESTS = (
    "package.json", "pyproject.toml", "requirements.txt", "app.json", "app.config.*", "eas.json",
    "wrangler.toml", "wrangler.json", "wrangler.jsonc", "config.toml", "Dockerfile", "docker-compose.*",
    "go.mod", "Cargo.toml", "pubspec.yaml", "composer.json", "Gemfile", "vercel.json", "netlify.toml",
)  # fmt: skip
_SIGNALS = re.compile(
    r"stripe|paddle|revenuecat|purchases|billing|pricing|checkout|subscription|paywall"
    r"|analytics|posthog|mixpanel|amplitude|segment|sentry|plausible",
    re.IGNORECASE,
)
_CODE_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".py", ".swift", ".kt", ".dart", ".go", ".rb"})
_MANIFEST_DEPTH = 2  # a manifest counts up to two folders down (apps/web/package.json)
_MAX_WORKFLOWS = 4
_MAX_SIGNALS = 6


def is_plain_path(path: str) -> bool:
    """A path as code hosts give it: forward slashes, no ``..`` part, no leading slash, no control codes."""
    if not _PLAIN.fullmatch(path) or path.startswith("/"):
        return False
    return all(part not in {"", ".", ".."} for part in path.split("/"))


def _plain(path: str) -> str:
    if not is_plain_path(path):
        raise ValueError("a file path must be plain: no '..', no leading slash, no control characters")
    return path


PlainPath = Annotated[str, AfterValidator(_plain)]


class _Shape(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RepoFile(_Shape):
    """One file the scout reads. ``text`` is untrusted; ``cut`` says the end was left off."""

    path: PlainPath
    text: str = Field(max_length=MAX_FILE_CHARS)
    cut: bool


class RepoBundle(_Shape):
    """One repository as the scout sees it: its paths, and the text of the files it picked.

    ``paths`` leaves out secret files (``secret_files`` counts them) and stops at
    :data:`MAX_PATHS` (``total_files`` counts every file that is not secret).
    """

    repo: str = Field(pattern=REPO_PATTERN)
    branch: str = Field(pattern=_BRANCH)
    paths: tuple[str, ...] = Field(max_length=MAX_PATHS)
    total_files: int = Field(ge=0)
    secret_files: int = Field(ge=0)
    files: tuple[RepoFile, ...] = Field(max_length=MAX_FILES)
    read_at: AwareDatetime

    @model_validator(mode="after")
    def _consistent(self) -> RepoBundle:
        listed = set(self.paths)
        if len(listed) != len(self.paths):
            raise ValueError("a path is listed twice")
        if self.total_files < len(self.paths):
            raise ValueError("total_files counts fewer files than are listed")
        if any(f.path not in listed for f in self.files):
            raise ValueError("a file was read that is not in the list")
        if any(is_secret_path(p) for p in self.paths):
            raise ValueError("a secret file is listed")
        if sum(len(f.text) for f in self.files) > MAX_TOTAL_CHARS:
            raise ValueError("the files hold more text than the scout may read")
        return self

    def missing(self, cited: Iterable[str]) -> tuple[str, ...]:
        """The cited paths that are not in this repository (for checking a model's answer)."""
        listed = set(self.paths)
        return tuple(p for p in cited if p not in listed)


def is_secret_path(path: str) -> bool:
    """True if the file is never read or listed: a key, an ``.env`` file, a database, a vendored folder."""
    parts = PurePosixPath(path).parts
    if any(part.lower() in _SECRET_FOLDERS for part in parts[:-1]):
        return True
    name = parts[-1].lower() if parts else ""
    return any(fnmatchcase(name, pattern) for pattern in _SECRET_NAMES)


def pick_files(paths: Sequence[str]) -> list[str]:
    """The files the scout reads, in reading order, at most :data:`MAX_FILES` (secret files never)."""
    usable = [p for p in paths if is_plain_path(p) and not is_secret_path(p)]
    by_depth = sorted(usable, key=lambda p: (p.count("/"), p.lower()))
    picked: list[str] = []

    def take(found: Iterable[str], limit: int = MAX_FILES) -> None:
        for path in list(found)[:limit]:
            if path not in picked and len(picked) < MAX_FILES:
                picked.append(path)

    take(p for p in by_depth if p.count("/") == 0 and _named(p, _ABOUT))
    take(p for p in by_depth if p.count("/") <= _MANIFEST_DEPTH and _named(p, _MANIFESTS))
    take(
        (p for p in by_depth if p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml"))),
        _MAX_WORKFLOWS,
    )
    take(
        (p for p in by_depth if PurePosixPath(p).suffix in _CODE_SUFFIXES and _SIGNALS.search(p)),
        _MAX_SIGNALS,
    )
    return picked


def _named(path: str, patterns: Sequence[str]) -> bool:
    name = PurePosixPath(path).name
    return any(fnmatchcase(name, pattern) for pattern in patterns)

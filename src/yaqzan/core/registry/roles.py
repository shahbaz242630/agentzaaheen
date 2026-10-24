"""The role library: role packs read from a folder outside the repository (ADR-039, F17 slice 3).

Role packs are our own instructions for each job (``docs/hr/roles`` on the
laptop, a folder on the server); they are never in the public repository.
Each pack is a folder of short Markdown files. A pack becomes an agent only if
it is an AI job: its job card's "Highest permission" starts with Read, Draft
or Act (the Board's own packs, Chairman and CEO, are people, not agents).

Every file is read with the registry's safe reader (size cap, no links, UTF-8).
A pack's ``version`` is the SHA-256 of its files' names and text, so the copy
stored when a role is given to a workspace can be checked against it later.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from yaqzan.core.registry.files import ItemError, read_text

Permission = Literal["read", "draft", "act"]

ROLE_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
REQUIRED = ("ROLE.md", "authority.md")
# Line limits from the role pack rules (docs/hr/README.md): small files an agent can hold in mind.
LINE_LIMITS = {
    "ROLE.md": 40,
    "authority.md": 60,
    "kpis.md": 50,
    "instructions.md": 80,
    "dos-and-donts.md": 50,
}
MAX_PACK_FILES = 40
_PERMISSION = re.compile(r"^\|\s*\*\*Highest permission\*\*\s*\|\s*([A-Za-z]+)", re.MULTILINE)
_TITLE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_MISSION = re.compile(r"^## Mission\s*\n+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class RolePack:
    """One role, ready to be given to a workspace."""

    id: str
    title: str
    mission: str
    permission: Permission
    files: dict[str, str]
    version: str


@dataclass(frozen=True)
class RoleProblem:
    """A pack that could not be used, and why, in plain English."""

    id: str
    code: str
    message: str


@dataclass(frozen=True)
class RoleLibrary:
    """Every usable pack, by id, and every pack that was refused."""

    packs: tuple[RolePack, ...]
    problems: tuple[RoleProblem, ...]

    def get(self, role_id: str) -> RolePack | None:
        """One pack by id, or ``None``."""
        return next((p for p in self.packs if p.id == role_id), None)


def version_of(files: dict[str, str]) -> str:
    """SHA-256 over every file's name and text, in a fixed order."""
    digest = hashlib.sha256()
    for name in sorted(files):
        data = files[name].encode("utf-8")
        digest.update(name.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0" + data)
    return digest.hexdigest()


def load_roles(folder: Path) -> RoleLibrary:
    """Every pack in ``folder`` (one sub-folder each; ``_``-folders are shared rules, not roles)."""
    packs: list[RolePack] = []
    problems: list[RoleProblem] = []
    if not folder.is_dir():
        return RoleLibrary((), (RoleProblem("", "role.no_library", f"{folder.name} is not a folder"),))
    for path in sorted(folder.iterdir(), key=lambda p: p.name):
        if not path.is_dir() or path.name.startswith(("_", ".")):
            continue
        try:
            packs.append(_pack(path))
        except ItemError as exc:
            problems.append(RoleProblem(path.name, exc.code, exc.message))
    return RoleLibrary(tuple(packs), tuple(problems))


def _pack(folder: Path) -> RolePack:
    if folder.is_symlink():
        raise ItemError(
            "registry.symlink", f"{folder.name} is a link to somewhere else; links are not followed"
        )
    if not ROLE_ID.fullmatch(folder.name) or len(folder.name) > 64:  # the database's limit
        raise ItemError(
            "role.bad_name", f"{folder.name} is not a usable role name (lower-case words and dashes)"
        )
    files = _files(folder)
    missing = [name for name in REQUIRED if name not in files]
    if missing:
        raise ItemError("role.incomplete", f"{folder.name} has no {' or '.join(missing)}")
    for name, limit in LINE_LIMITS.items():
        if name in files and files[name].count("\n") > limit:
            raise ItemError("role.too_long", f"{name} is over {limit} lines; split it into a sub-folder")
    card = files["ROLE.md"]
    return RolePack(
        id=folder.name,
        title=_first(_TITLE, card, folder.name),
        mission=_first(_MISSION, card, ""),
        permission=_permission(card, folder.name),
        files=files,
        version=version_of(files),
    )


def _files(folder: Path, prefix: str = "") -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(folder.iterdir(), key=lambda p: p.name):
        name = f"{prefix}{path.name}"
        if path.is_symlink():
            raise ItemError("registry.symlink", f"{name} is a link to somewhere else; links are not followed")
        if path.is_dir():
            found.update(_files(path, f"{name}/"))
        elif path.suffix != ".md":
            raise ItemError("role.not_markdown", f"{name} is not a Markdown file")
        else:
            found[name] = read_text(path)
        if len(found) > MAX_PACK_FILES:
            raise ItemError("role.too_many_files", f"the pack has more than {MAX_PACK_FILES} files")
    return found


def _permission(card: str, role_id: str) -> Permission:
    match = _PERMISSION.search(card)
    word = match.group(1).lower() if match else ""
    if word == "read":
        return "read"
    if word == "draft":
        return "draft"
    if word == "act":
        return "act"
    raise ItemError(
        "role.not_an_agent", f"{role_id} is not an AI job (its highest permission is not Read, Draft or Act)"
    )


def _first(pattern: re.Pattern[str], text: str, default: str) -> str:
    match = pattern.search(text)
    return match.group(1)[:200] if match else default

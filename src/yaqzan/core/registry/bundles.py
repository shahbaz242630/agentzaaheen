"""Reading one plugin bundle in Claude's, Codex's or Cursor's layout (``.<app>-plugin/plugin.json``).

Read only: nothing in a bundle is run, installed or switched on here. We
count what it would bring (skills, connectors, commands, agents, hooks) so the
Customize page can show it. Every path the manifest names must stay inside
the bundle (``./``-relative, no ``..``), and a bundle whose connector config
holds a secret in plain text is refused (research/07 §4).
"""

from __future__ import annotations

import re
from pathlib import Path

from yaqzan.core.registry.files import ItemError, fingerprint, load_mapping, read_text, refuse_secrets
from yaqzan.core.registry.models import Layout, Plugin

MANIFEST = "plugin.json"
MCP_FILE = ".mcp.json"
PARTS: dict[str, str] = {"skills": "*/SKILL.md", "commands": "*.md", "agents": "*.md"}
_NAME = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
MAX_NAME = 64


def bundle_layout(folder: Path) -> Layout | None:
    """Which layout ``folder`` uses, or ``None`` if it is not a bundle (Claude's is checked first)."""
    for layout in Layout:
        if (folder / f".{layout.value}-plugin" / MANIFEST).is_file():
            return layout
    return None


def read_bundle(folder: Path) -> Plugin:
    """The bundle in ``folder``, or :class:`ItemError` saying why it was refused."""
    layout = bundle_layout(folder)
    if layout is None:
        raise ItemError("registry.no_manifest", f"the folder has no .<app>-plugin/{MANIFEST}")
    fields = load_mapping(read_text(folder / f".{layout.value}-plugin" / MANIFEST), MANIFEST)
    refuse_secrets(fields, MANIFEST)
    name = fields.get("name")
    if not isinstance(name, str) or len(name) > MAX_NAME or not _NAME.match(name):
        raise ItemError(
            "registry.bad_name", "the plugin's name must be lowercase words joined by single hyphens"
        )
    counts = {part: _count(folder, part, pattern, fields.get(part)) for part, pattern in PARTS.items()}
    return Plugin(
        id=name,
        description=_text(fields.get("description"), "description", 1024),
        version=_text(fields.get("version"), "version", 40) or None,
        layout=layout,
        skills=counts["skills"],
        commands=counts["commands"],
        agents=counts["agents"],
        connectors=_servers(folder, fields.get("mcpServers")),
        hooks=_hooks(folder, fields.get("hooks")),
        fingerprint=fingerprint(folder),
    )


def _text(value: object, field: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise ItemError(
            "registry.bad_manifest", f"{MANIFEST}: '{field}' must be text of at most {limit} characters"
        )
    return " ".join(value.split())


def _inside(folder: Path, declared: str) -> Path:
    """``declared`` resolved inside ``folder``; anything that could point outside it is refused."""
    if not declared.startswith("./") or "\\" in declared or ".." in declared.split("/"):
        raise ItemError(
            "registry.path_escape",
            f"{MANIFEST} names the path '{declared}'; paths must start with ./ and stay inside",
        )
    path = (folder / declared).resolve()
    if not path.is_relative_to(folder.resolve()):
        raise ItemError(
            "registry.path_escape", f"{MANIFEST} names the path '{declared}', which leads outside the bundle"
        )
    return path


def _declared(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    if isinstance(value, dict):  # Codex/open-plugins: {"paths": [...]}
        return _declared(value.get("paths"))
    return []


def _count(folder: Path, part: str, pattern: str, declared: object) -> int:
    places = {(folder / part).resolve()} | {_inside(folder, path) for path in _declared(declared)}
    found = {match for place in places if place.is_dir() for match in place.glob(pattern) if match.is_file()}
    return len(found)


def _hooks(folder: Path, declared: object) -> bool:
    """Whether the bundle brings hooks (code that runs on events: never run here, only shown)."""
    if not isinstance(declared, dict):
        for path in _declared(declared):
            _inside(folder, path)
    return (folder / "hooks" / "hooks.json").is_file() or bool(declared)


def _servers(folder: Path, declared: object) -> int:
    """How many connector (MCP) servers the bundle defines, inline or in ``.mcp.json``."""
    if isinstance(declared, dict):
        return len(declared)
    path = _inside(folder, declared) if isinstance(declared, str) else folder / MCP_FILE
    if not path.is_file():
        return 0
    config = load_mapping(read_text(path), path.name)
    refuse_secrets(config, path.name)
    servers = config.get("mcpServers", {})
    return len(servers) if isinstance(servers, dict) else 0

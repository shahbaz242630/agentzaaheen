"""The registry: one list of every connector, skill and plugin bundle Yaqzan has (ADR-027, F06).

What it does:
    Reads, from disk only, each connector's reviewed ``plugin.yaml``, each
    skill's ``SKILL.md`` (open Agent Skills format) and any plugin bundles in
    Claude's, Codex's or Cursor's layout, and returns one :class:`Registry`.
    Each item gets a fingerprint (SHA-256 of its files) so a later approval
    can be tied to exactly what was reviewed. Items that cannot be read are
    kept as :class:`Problem` entries with a plain-English reason; the rest
    still load. Design evidence: research/06 and research/07.

Public interface:
    ``load_registry`` and the shapes ``Registry``, ``Connector``, ``Skill``,
    ``Plugin``, ``Problem``, ``Kind``, ``Invocation``, ``Layout``.

How to test:
    ``uv run pytest tests/unit/test_registry_files.py tests/unit/test_registry_items.py``
    ``uv run pytest tests/unit/test_registry_load.py``

Must never:
    Run, install, import or switch on anything it reads; follow a symlink or a
    path that leads outside an item's folder; accept a manifest holding
    something that looks like a secret; trust what an item says about its own
    safety beyond our reviewed manifest; let two items share an id; touch the
    database, the network or the vault.
"""

from yaqzan.core.registry.load import load_registry
from yaqzan.core.registry.models import Connector, Invocation, Kind, Layout, Plugin, Problem, Registry, Skill

__all__ = [
    "Connector",
    "Invocation",
    "Kind",
    "Layout",
    "Plugin",
    "Problem",
    "Registry",
    "Skill",
    "load_registry",
]

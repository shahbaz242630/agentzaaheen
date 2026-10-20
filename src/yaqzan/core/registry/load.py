"""Building the registry from disk: every item read on its own, problems kept, clashes refused.

One bad item never stops the others loading (it becomes a :class:`Problem`
with a plain-English reason). Two items of the same kind with the same id are
*both* refused: neither may silently replace the other (research/07 §3.6).

One exception, for plugin bundles only: Claude Code and Codex keep each
installed version in its own folder side by side
(``<marketplace>/<plugin>/<version>/``). Bundles with the same name in the
same parent folder are versions of one plugin, so only the newest copy (by
the time its manifest was written) is shown. Same name in *different*
folders is still a clash.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from yaqzan.core.registry.bundles import bundle_layout, read_bundle
from yaqzan.core.registry.connectors import read_connector
from yaqzan.core.registry.files import SKIPPED_DIRS, ItemError
from yaqzan.core.registry.models import Connector, Kind, Plugin, Problem, Registry, Skill
from yaqzan.core.registry.skills import read_skill
from yaqzan.core.sdk import get_logger

BUNDLE_SEARCH_DEPTH = 3  # e.g. <marketplace>/<plugin>/<version>/ in Claude Code's plugin cache
_SKIPPED_SEARCH = SKIPPED_DIRS | {"node_modules"}


def load_registry(plugins_root: Path, bundle_roots: Sequence[Path] = ()) -> Registry:
    """Read ``connectors/`` and ``skills/`` under ``plugins_root``, and bundles under ``bundle_roots``."""
    problems: list[Problem] = []
    connectors = _read_all(
        Kind.CONNECTOR, _subfolders(plugins_root / "connectors"), read_connector, plugins_root, problems
    )
    skills = _read_all(Kind.SKILL, _subfolders(plugins_root / "skills"), read_skill, plugins_root, problems)
    plugins: list[tuple[Plugin, str]] = []
    for root in bundle_roots:
        found = _read_all(
            Kind.PLUGIN, _bundles(root, BUNDLE_SEARCH_DEPTH), read_bundle, root.parent, problems
        )
        plugins += _newest_versions(found, root.parent)
    kept_connectors = _without_clashes(Kind.CONNECTOR, connectors, problems)
    kept_skills = _without_clashes(Kind.SKILL, skills, problems)
    kept_plugins = _without_clashes(Kind.PLUGIN, plugins, problems)
    registry = Registry(
        connectors=kept_connectors, skills=kept_skills, plugins=kept_plugins, problems=tuple(problems)
    )
    log = get_logger("yaqzan.registry")
    for problem in registry.problems:
        log.warning("registry_item_refused", kind=problem.kind.value, path=problem.path, code=problem.code)
    return registry


def _subfolders(parent: Path) -> Iterator[Path]:
    if not parent.is_dir():
        return
    for path in sorted(parent.iterdir()):
        if path.is_dir() and not path.name.startswith(("_", ".")):
            yield path


def _bundles(folder: Path, depth: int) -> Iterator[Path]:
    """Bundle folders at or under ``folder``, at most ``depth`` levels down (never inside a bundle)."""
    if not folder.is_dir() or folder.is_symlink():
        return
    if bundle_layout(folder) is not None:
        yield folder
        return
    if depth > 0:
        for path in sorted(folder.iterdir()):
            if path.is_dir() and path.name not in _SKIPPED_SEARCH:
                yield from _bundles(path, depth - 1)


def _read_all[Item: (Connector, Skill, Plugin)](
    kind: Kind,
    folders: Iterator[Path],
    read: Callable[[Path], Item],
    base: Path,
    problems: list[Problem],
) -> list[tuple[Item, str]]:
    found: list[tuple[Item, str]] = []
    for folder in folders:
        where = folder.relative_to(base).as_posix()
        try:
            found.append((read(folder), where))
        except ItemError as exc:
            problems.append(Problem(kind=kind, path=where, code=exc.code, message=exc.message))
        except OSError:
            problems.append(
                Problem(
                    kind=kind, path=where, code="registry.unreadable", message="the folder could not be read"
                )
            )
    return found


def _newest_versions(found: Sequence[tuple[Plugin, str]], base: Path) -> list[tuple[Plugin, str]]:
    """One entry per plugin per parent folder: side-by-side version folders collapse to the newest."""
    newest: dict[tuple[str, str], tuple[float, str, Plugin]] = {}
    for plugin, where in found:
        parent = where.rpartition("/")[0]
        written = (base / where / f".{plugin.layout.value}-plugin" / "plugin.json").stat().st_mtime
        current = newest.get((plugin.id, parent))
        if current is None or (written, where) > current[:2]:
            newest[(plugin.id, parent)] = (written, where, plugin)
    return [(plugin, where) for _, where, plugin in newest.values()]


def _without_clashes[Item: (Connector, Skill, Plugin)](
    kind: Kind, pairs: Sequence[tuple[Item, str]], problems: list[Problem]
) -> tuple[Item, ...]:
    counts = Counter(item.id for item, _ in pairs)
    kept: list[Item] = []
    for item, where in pairs:
        if counts[item.id] > 1:
            message = f"another {kind.value} is also called '{item.id}', so neither was loaded"
            problems.append(Problem(kind=kind, path=where, code="registry.duplicate_id", message=message))
        else:
            kept.append(item)
    return tuple(sorted(kept, key=lambda item: item.id))

"""Only a door opens a handle for one workspace (ADR-039, F17 slice 2a).

The database walls stop a query that forgets its workspace; this stops code
that names the wrong one. A handle for one workspace (``db.scoped(tenant,
workspace)`` or ``handle.within(workspace)``) may be made only where the
workspace is chosen once: the places listed here. Everything further in is
given a handle and cannot pick another. A new place that opens one must be
added here on purpose, in review.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "yaqzan"

# Where a workspace handle may be opened, and why.
DOORS = {
    # Tenant-wide reads go through every workspace of the tenant, one at a time.
    "src/yaqzan/core/store/sealed.py",
    # The daily key check is a tenant-wide job (the Board's), one workspace at a time.
    "src/yaqzan/core/scheduler/keys.py",
    # Agents: the workspace comes from the API door's address or the job's own saved record (may_run).
    "src/yaqzan/core/policy/agents.py",
    # Removing a project: the workspace comes from the API door's address (archive) or the archived list.
    "src/yaqzan/core/policy/workspaces.py",
}


def _opens_a_workspace(call: ast.Call) -> bool:
    if not isinstance(call.func, ast.Attribute):
        return False
    if call.func.attr == "within":
        return True
    named = {k.arg for k in call.keywords}
    return call.func.attr == "scoped" and (len(call.args) >= 2 or "workspace_id" in named)


def _openers() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in [*SRC.rglob("*.py"), *(ROOT / "tools").rglob("*.py")]:
        rel = path.relative_to(ROOT).as_posix()
        if rel == "src/yaqzan/core/store/db.py":  # the handle itself
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        count = sum(1 for n in ast.walk(tree) if isinstance(n, ast.Call) and _opens_a_workspace(n))
        if count:
            found[rel] = count
    return found


def test_only_the_doors_open_a_handle_for_one_workspace() -> None:
    assert set(_openers()) - DOORS == set()


def test_each_door_still_opens_one() -> None:
    """A door that no longer opens a handle should come off the list (and the check stays meaningful)."""
    assert set(_openers()) == DOORS

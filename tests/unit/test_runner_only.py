"""No code path starts an agent outside the runner (ADR-036, F11 slice 1).

An agent can only be run with a ticket, and a ticket only carries the runner's
seal when the runner made it. Two checks: making a ticket by hand fails, and
no file outside ``core/runner`` uses the runner's private ticket maker or seal.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from agentzaaheen.core.policy import Budget
from agentzaaheen.core.runner import RunnerOnlyError, RunTicket

ROOT = Path(__file__).resolve().parents[2]
RUNNER = "src/agentzaaheen/core/runner/"
PRIVATE = {"_mint", "_RUNNER"}


def _ticket(seal: object) -> RunTicket:
    return RunTicket(
        run_id=uuid4(),
        workspace_id=uuid4(),
        agent_id=uuid4(),
        role_id="monitoring",
        version="f" * 64,
        pack={"ROLE.md": "# Monitoring"},
        tools=frozenset(),
        budget=Budget(steps=1, tokens=1, seconds=1),
        started_at=datetime(2026, 10, 26, tzinfo=UTC),
        model=None,
        seal=seal,
    )


@pytest.mark.parametrize("seal", [object(), None, "runner"])
def test_a_ticket_made_by_hand_is_refused(seal: object) -> None:
    with pytest.raises(RunnerOnlyError):
        _ticket(seal)


def _uses_private_names(path: Path) -> set[str]:
    used: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Name) and node.id in PRIVATE:
            used.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in PRIVATE:
            used.add(node.attr)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("agentzaaheen.core.runner")
        ):
            used.update(alias.name for alias in node.names if alias.name in PRIVATE)
    return used


def test_only_the_runner_holds_its_seal() -> None:
    files = [*(ROOT / "src").rglob("*.py"), *(ROOT / "tools").rglob("*.py"), *(ROOT / "tests").rglob("*.py")]
    assert len(files) > 100  # the check below would pass vacuously if this found nothing
    outside = {
        rel: names
        for path in files
        if not (rel := path.relative_to(ROOT).as_posix()).startswith(RUNNER)
        and rel != "tests/unit/test_runner_only.py"
        and (names := _uses_private_names(path))
    }
    assert outside == {}


def test_the_runner_itself_uses_the_seal() -> None:
    # Proves the scan above can see a use: the runner's own files must show one.
    assert _uses_private_names(ROOT / RUNNER / "run.py") == {"_mint"}

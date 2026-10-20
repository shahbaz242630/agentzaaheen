"""The code-health shape: its build fields must add up, and it holds nothing people wrote."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from yaqzan.core.sdk import BuildState, RepoHealth

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
SHA = "c" * 40


def health(**changes: object) -> RepoHealth:
    base: dict[str, object] = {
        "source": "github",
        "repo": "acme-labs/widget",
        "default_branch": "main",
        "open_issues": 3,
        "open_pull_requests": 1,
        "build": BuildState.PASSED,
        "build_commit": SHA,
        "failing_workflows": 0,
        "build_finished_at": NOW,
        "fetched_at": NOW,
    }
    return RepoHealth.model_validate({**base, **changes})


def test_a_consistent_health_is_accepted() -> None:
    assert health().build is BuildState.PASSED
    assert health(build="failed", failing_workflows=2).failing_workflows == 2
    assert health(build="running", build_finished_at=None).build_finished_at is None
    assert health(build="none", build_commit=None, build_finished_at=None).build_commit is None


@pytest.mark.parametrize(
    "changes",
    [
        {"build": "none"},
        {"build": "none", "build_commit": None},
        {"build_commit": None},
        {"failing_workflows": 1},
        {"build": "failed"},
        {"build": "running"},
        {"build_finished_at": None},
        {"open_issues": -1},
        {"repo": "widget"},
        {"repo": "acme labs/widget"},
        {"default_branch": "main branch"},
        {"build_commit": "C" * 40},
        {"fetched_at": datetime(2026, 10, 20, 12)},  # noqa: DTZ001  # naive on purpose
        {"title": "not a field"},
    ],
)
def test_inconsistent_or_foreign_fields_are_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        health(**changes)

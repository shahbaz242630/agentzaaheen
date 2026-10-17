from __future__ import annotations

from pathlib import Path

import pytest

from tools import git_hooks


def test_usage_on_bad_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    assert git_hooks.main([]) == 2
    assert git_hooks.main(["nonsense"]) == 2
    assert "usage" in capsys.readouterr().out


def test_install_writes_both_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    monkeypatch.setattr(git_hooks, "ROOT", tmp_path)
    assert git_hooks.main(["install"]) == 0
    for stage in ("pre-commit", "pre-push"):
        text = (tmp_path / ".git" / "hooks" / stage).read_text(encoding="utf-8")
        assert f"tools.git_hooks {stage}" in text


def test_install_outside_a_clone_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(git_hooks, "ROOT", tmp_path)
    assert git_hooks.main(["install"]) == 1


def test_pre_commit_refuses_without_gitleaks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tools.git_hooks.shutil.which", lambda _name: None)
    assert git_hooks.main(["pre-commit"]) == 1


@pytest.mark.parametrize(("codes", "expected"), [([0, 0, 0, 0], 0), ([0, 1, 0, 0], 1)])
def test_pre_commit_stops_at_first_failure(
    monkeypatch: pytest.MonkeyPatch, codes: list[int], expected: int
) -> None:
    calls: list[list[str]] = []
    results = iter(codes)

    def fake_run(argv: list[str]) -> int:
        calls.append(argv)
        return next(results)

    monkeypatch.setattr("tools.git_hooks.shutil.which", lambda _name: "gitleaks")
    monkeypatch.setattr(git_hooks, "_run", fake_run)
    assert git_hooks.main(["pre-commit"]) == expected
    assert "tools.path_guard" in calls[0]
    if expected:
        assert len(calls) == 2


@pytest.mark.parametrize(("codes", "expected"), [([0, 0], 0), ([1], 1)])
def test_pre_push_runs_tests_then_coverage(
    monkeypatch: pytest.MonkeyPatch, codes: list[int], expected: int
) -> None:
    results = iter(codes)
    monkeypatch.setattr(git_hooks, "_run", lambda _argv: next(results))
    assert git_hooks.main(["pre-push"]) == expected


def test_run_returns_exit_code() -> None:
    assert git_hooks._run(["git", "--version"]) == 0

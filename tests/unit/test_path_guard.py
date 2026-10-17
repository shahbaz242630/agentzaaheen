from __future__ import annotations

import pytest

from tools import path_guard


@pytest.mark.parametrize(
    "path",
    [
        "src/yaqzan/__init__.py",
        "src/yaqzan/core/sdk/models.py",
        "src/yaqzan/py.typed",
        "tests/unit/test_sdk_models.py",
        "tools/path_guard.py",
        "src/yaqzan/plugins/agents/support/prompt.md",
        "src/yaqzan/plugins/skills/triage/SKILL.md",
        "src/yaqzan/plugins/connectors/paddle/plugin.yaml",
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        ".github/workflows/ci.yml",
        "./pyproject.toml",
        "src\\yaqzan\\core\\sdk\\models.py",
    ],
)
def test_code_is_allowed(path: str) -> None:
    assert path_guard.check([path]) == []


@pytest.mark.parametrize(
    "path",
    [
        # documentation and admin material (ADR-014)
        "README.md",
        "docs/02-PRD.md",
        "HANDOFF.md",
        "research/03-market.md",
        "runbooks/site-down.md",
        "src/yaqzan/core/README.md",
        "src/yaqzan/plugins/agents/support/notes.md",
        "architecture.html",
        "plan.pdf",
        # secrets
        ".env",
        ".env.production",
        "src/yaqzan/core/vault/master.key",
        "deploy/tls.pem",
        "AuthKey_ABC123.p8",
        # real configs
        "projects/zaaheen/project.yaml",
        "src/yaqzan/plugins/connectors/paddle/config.yaml",
        # anything unknown
        "random.txt",
        ".github/workflows/ci.yaml.bak",
        "src/yaqzan/plugins/agents/support/extra/prompt.md",
    ],
)
def test_everything_else_is_rejected(path: str) -> None:
    assert path_guard.check([path]) == [path.replace("\\", "/")]


def test_mixed_list_reports_only_bad_paths() -> None:
    assert path_guard.check(["pyproject.toml", "docs/BRD.md", "", "src/yaqzan/x.py"]) == ["docs/BRD.md"]


def test_cli_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    assert path_guard.main(["pyproject.toml"]) == 0
    assert path_guard.main(["pyproject.toml", "docs/PRD.md"]) == 1
    out = capsys.readouterr().out
    assert "docs/PRD.md" in out
    assert "code only" in out


class _FakeCompleted:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def test_staged_and_tracked_modes_ask_git(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> _FakeCompleted:
        calls.append(argv)
        return _FakeCompleted("pyproject.toml\n\nsrc/yaqzan/a.py\n")

    monkeypatch.setattr("tools.path_guard.subprocess.run", fake_run)
    assert path_guard.main(["--staged"]) == 0
    assert path_guard.main(["--tracked"]) == 0
    assert calls[0][:2] == ["git", "diff"]
    assert calls[1] == ["git", "ls-files"]

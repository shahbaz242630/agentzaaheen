import json
import os
from pathlib import Path

import pytest

from agentzaaheen.core.registry import Kind, Registry, load_registry
from agentzaaheen.core.registry.models import Skill

PLUGINS = Path(__file__).resolve().parents[2] / "src" / "agentzaaheen" / "plugins"


def write_skill(root: Path, name: str, description: str = "Does a thing.") -> None:
    folder = root / "skills" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nSteps\n", encoding="utf-8"
    )


def write_bundle(folder: Path, name: str, layout: str = "claude") -> None:
    (folder / f".{layout}-plugin").mkdir(parents=True)
    (folder / f".{layout}-plugin" / "plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")


def test_the_real_plugins_folder_loads_without_problems() -> None:
    registry = load_registry(PLUGINS)
    assert [c.id for c in registry.connectors] == [
        "availability",
        "expiry",
        "github",
        "healthchecks",
        "paddle",
        "supabase",
        "uptime",
    ]
    assert [s.id for s in registry.skills] == [
        "check-slo",
        "read-logs",
        "trace-request",
        "triage-failing-build",
        "triage-outage",
    ]
    assert registry.plugins == ()
    assert registry.problems == ()


def test_a_missing_plugins_folder_is_simply_empty(tmp_path: Path) -> None:
    assert load_registry(tmp_path / "nowhere", [tmp_path / "nor-here"]) == Registry()


def test_one_bad_item_does_not_stop_the_others(tmp_path: Path) -> None:
    write_skill(tmp_path, "good-one")
    write_skill(tmp_path, "bad-one", "use <b>")
    (tmp_path / "skills" / "empty").mkdir()
    registry = load_registry(tmp_path)
    assert [s.id for s in registry.skills] == ["good-one"]
    assert [(p.kind, p.path, p.code) for p in registry.problems] == [
        (Kind.SKILL, "skills/bad-one", "registry.bad_description"),
        (Kind.SKILL, "skills/empty", "registry.no_manifest"),
    ]


def test_private_and_cache_folders_are_skipped(tmp_path: Path) -> None:
    write_skill(tmp_path, "good-one")
    (tmp_path / "skills" / "__pycache__").mkdir()
    (tmp_path / "skills" / ".hidden").mkdir()
    (tmp_path / "skills" / "__init__.py").write_text("", encoding="utf-8")
    registry = load_registry(tmp_path)
    assert [s.id for s in registry.skills] == ["good-one"]
    assert registry.problems == ()


def test_an_unreadable_folder_becomes_a_problem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_skill(tmp_path, "good-one")

    def broken(folder: Path) -> Skill:
        raise PermissionError(folder)

    monkeypatch.setattr("agentzaaheen.core.registry.load.read_skill", broken)
    problems = load_registry(tmp_path).problems
    assert [(p.code, p.message) for p in problems] == [
        ("registry.unreadable", "the folder could not be read")
    ]


def test_two_bundles_with_one_name_in_different_folders_are_both_refused(tmp_path: Path) -> None:
    root = tmp_path / "bundles"
    write_bundle(root / "market-a" / "kit", "kit")
    write_bundle(root / "market-b" / "kit", "kit", "codex")
    write_bundle(root / "market-a" / "other", "other")
    registry = load_registry(tmp_path / "plugins", [root])
    assert [p.id for p in registry.plugins] == ["other"]
    assert sorted(p.path for p in registry.problems) == ["bundles/market-a/kit", "bundles/market-b/kit"]
    assert {p.code for p in registry.problems} == {"registry.duplicate_id"}
    assert all("neither was loaded" in p.message for p in registry.problems)


def test_side_by_side_versions_of_one_plugin_show_the_newest(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    for version, written in (("1.0.0", 1_000), ("2.0.0", 2_000), ("1.5.0", 3_000)):
        write_bundle(root / "market" / "kit" / version, "kit")
        manifest = root / "market" / "kit" / version / ".claude-plugin" / "plugin.json"
        manifest.write_text(json.dumps({"name": "kit", "version": version}), encoding="utf-8")
        os.utime(manifest, (written, written))
    registry = load_registry(tmp_path / "plugins", [root])
    assert [(p.id, p.version) for p in registry.plugins] == [("kit", "1.5.0")]  # newest written, not highest
    assert registry.problems == ()


def test_bundles_are_found_a_few_levels_down_but_not_inside_a_bundle(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    write_bundle(root / "market" / "kit" / "1.0.0", "kit")
    write_bundle(root / "market" / "kit" / "1.0.0" / "nested", "nested")
    write_bundle(root / "a" / "b" / "c" / "too-deep", "deep")
    write_bundle(root / "node_modules" / "x", "npm-thing")
    registry = load_registry(tmp_path / "plugins", [root])
    assert [p.id for p in registry.plugins] == ["kit"]


def test_problems_are_logged_without_their_contents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_skill(tmp_path, "leaky", "token ghp_" + "a" * 36)
    load_registry(tmp_path)
    out = capsys.readouterr().err
    assert "registry_item_refused" in out
    assert "registry.secret_in_manifest" in out
    assert "ghp_" not in out


def test_the_registry_refuses_two_items_with_one_id() -> None:
    skill = Skill(id="a", description="x", invocation="auto", requires=(), fingerprint="0" * 64)
    with pytest.raises(ValueError, match="skill ids must be unique"):
        Registry(skills=(skill, skill))

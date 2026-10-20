import json
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

from yaqzan.core.registry import Invocation, Layout
from yaqzan.core.registry.bundles import read_bundle
from yaqzan.core.registry.connectors import read_connector
from yaqzan.core.registry.files import ItemError
from yaqzan.core.registry.skills import read_skill
from yaqzan.core.sdk import ToolLevel

PLUGINS = Path(__file__).resolve().parents[2] / "src" / "yaqzan" / "plugins"


def refused(call: object) -> ItemError:
    assert callable(call)
    with pytest.raises(ItemError) as caught:
        call()
    return caught.value


# ---------------------------------------------------------------- skills


def skill(root: Path, header: str, body: str = "# Do it\n\nSteps.\n", name: str = "fix-build") -> Path:
    folder = root / name
    folder.mkdir()
    (folder / "SKILL.md").write_text(f"---\n{header}---\n{body}", encoding="utf-8")
    return folder


GOOD = "name: fix-build\ndescription: Find out why a build failed.\n"


def test_a_plain_skill_runs_when_needed_and_needs_nothing(tmp_path: Path) -> None:
    found = read_skill(skill(tmp_path, GOOD))
    assert (found.id, found.invocation, found.requires) == ("fix-build", Invocation.AUTO, ())
    assert len(found.fingerprint) == 64


def test_our_fields_live_under_metadata(tmp_path: Path) -> None:
    header = GOOD + "metadata:\n  requires: github, paddle, github\n  invocation: Manual\n"
    found = read_skill(skill(tmp_path, header))
    assert found.requires == ("github", "paddle")
    assert found.invocation is Invocation.MANUAL


def test_claude_codes_manual_flag_is_understood(tmp_path: Path) -> None:
    header = GOOD + "disable-model-invocation: true\nmetadata:\n  invocation: auto\n"
    assert read_skill(skill(tmp_path, header)).invocation is Invocation.MANUAL


def test_the_formats_optional_fields_are_allowed(tmp_path: Path) -> None:
    header = GOOD + "license: Apache-2.0\nallowed-tools: Read\ncompatibility: any\n"
    assert read_skill(skill(tmp_path, header)).id == "fix-build"


def test_a_third_party_description_with_a_colon_is_read(tmp_path: Path) -> None:
    header = "name: fix-build\ndescription: Build for AWS: ECS and Fargate\n"
    assert read_skill(skill(tmp_path, header)).description == "Build for AWS: ECS and Fargate"


def test_a_long_folded_description_becomes_one_line(tmp_path: Path) -> None:
    header = "name: fix-build\ndescription: >\n  Find out why\n  a build failed.\n"
    assert read_skill(skill(tmp_path, header)).description == "Find out why a build failed."


def test_a_windows_edited_skill_with_a_bom_is_read(tmp_path: Path) -> None:
    folder = tmp_path / "fix-build"
    folder.mkdir()
    (folder / "SKILL.md").write_bytes(("﻿---\r\n" + GOOD.replace("\n", "\r\n") + "---\r\nSteps\r\n").encode())
    assert read_skill(folder).id == "fix-build"


@pytest.mark.parametrize(
    ("header", "code"),
    [
        (GOOD + "requires: github\n", "registry.unknown_field"),
        ("name: Fix_Build\ndescription: x\n", "registry.bad_name"),
        ("name: fix--build\ndescription: x\n", "registry.bad_name"),
        (f"name: {'a' * 65}\ndescription: x\n", "registry.bad_name"),
        ("name: other\ndescription: x\n", "registry.name_mismatch"),
        ("name: fix-build\n", "registry.no_description"),
        ("name: fix-build\ndescription: '   '\n", "registry.no_description"),
        ("name: fix-build\ndescription: use <script>\n", "registry.bad_description"),
        (f"name: fix-build\ndescription: {'a' * 1025}\n", "registry.bad_description"),
        (GOOD + "metadata:\n  requires: [github]\n", "registry.bad_metadata"),
        (GOOD + "metadata: plain\n", "registry.bad_metadata"),
        (GOOD + "metadata:\n  invocation: always\n", "registry.bad_metadata"),
        (GOOD + "metadata:\n  requires: GitHub\n", "registry.bad_metadata"),
        (GOOD + "disable-model-invocation: maybe\n", "registry.bad_metadata"),
        (GOOD + "metadata:\n  api_key: abc123\n", "registry.secret_in_manifest"),
    ],
)
def test_a_bad_skill_header_is_refused_with_a_reason(tmp_path: Path, header: str, code: str) -> None:
    error = refused(lambda: read_skill(skill(tmp_path, header)))
    assert error.code == code
    assert error.message


@pytest.mark.parametrize(
    ("body", "code"),
    [("\n  \n", "registry.empty_skill"), ("line\n" * 501, "registry.too_long")],
)
def test_a_skill_must_have_a_body_of_sensible_length(tmp_path: Path, body: str, code: str) -> None:
    assert refused(lambda: read_skill(skill(tmp_path, GOOD, body))).code == code


def test_a_folder_without_skill_md_is_refused(tmp_path: Path) -> None:
    assert refused(lambda: read_skill(tmp_path)).code == "registry.no_manifest"


def test_the_first_skill_is_valid_and_needs_github() -> None:
    found = read_skill(PLUGINS / "skills" / "triage-failing-build")
    assert found.requires == ("github",)
    assert found.invocation is Invocation.AUTO


# ---------------------------------------------------------------- connectors


def manifest(**changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "acme",
        "type": "connector",
        "version": "0.1.0",
        "sdk": ">=1.5,<2",
        "permissions": ["read"],
        "egress": ["api.acme.test"],
        "endpoints": ["GET /items"],
        "credentials": [{"name": "token", "scope_hint": "read-only", "verify": True}],
        "data_class": "internal",
        "interface": {"display_name": "Acme", "category": "Ops", "description": "Reads items."},
    }
    base.update(changes)
    return {key: value for key, value in base.items() if value is not None}


def connector(root: Path, fields: dict[str, object], name: str = "acme") -> Path:
    folder = root / name
    folder.mkdir()
    (folder / "plugin.yaml").write_text(yaml.safe_dump(fields), encoding="utf-8")
    return folder


@pytest.mark.parametrize("name", ["paddle", "github"])
def test_our_live_connectors_are_read_only(name: str) -> None:
    found = read_connector(PLUGINS / "connectors" / name)
    assert found.level is ToolLevel.READ
    assert all(endpoint.startswith("GET /") for endpoint in found.endpoints)
    assert found.key_slots


def test_a_connector_keeps_slot_names_only(tmp_path: Path) -> None:
    found = read_connector(connector(tmp_path, manifest()))
    assert (found.name, found.category, found.key_slots, found.hosts) == (
        "Acme",
        "Ops",
        ("token",),
        ("api.acme.test",),
    )


def test_undeclared_permissions_count_as_act(tmp_path: Path) -> None:
    found = read_connector(connector(tmp_path, manifest(permissions=None, endpoints=["POST /send"])))
    assert found.level is ToolLevel.ACT


def test_the_highest_declared_permission_wins(tmp_path: Path) -> None:
    fields = manifest(permissions=["read", "draft"], endpoints=["GET /a", "POST /b"])
    assert read_connector(connector(tmp_path, fields)).level is ToolLevel.DRAFT


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        (manifest(endpoints=["GET /items", "DELETE /items/{id}"]), "registry.bad_manifest"),
        (manifest(endpoints=["items"]), "registry.bad_manifest"),
        (manifest(permissions=[]), "registry.bad_manifest"),
        (manifest(interface=None), "registry.bad_manifest"),
        (manifest(type="agent"), "registry.bad_manifest"),
        (manifest(surprise=1), "registry.bad_manifest"),
        (manifest(id="other"), "registry.name_mismatch"),
        (
            manifest(credentials=[{"name": "token", "scope_hint": "ghp_" + "a" * 36}]),
            "registry.secret_in_manifest",
        ),
    ],
)
def test_a_bad_connector_is_refused_with_a_reason(
    tmp_path: Path, fields: dict[str, object], code: str
) -> None:
    assert refused(lambda: read_connector(connector(tmp_path, fields))).code == code


def test_a_write_endpoint_in_a_read_only_connector_is_named(tmp_path: Path) -> None:
    error = refused(lambda: read_connector(connector(tmp_path, manifest(endpoints=["PATCH /items"]))))
    assert "read-only" in error.message


def test_a_connector_folder_without_a_manifest_is_refused(tmp_path: Path) -> None:
    assert refused(lambda: read_connector(tmp_path)).code == "registry.no_manifest"


# ---------------------------------------------------------------- plugin bundles


def bundle(root: Path, fields: Mapping[str, object], layout: str = "claude", name: str = "kit") -> Path:
    folder = root / name
    (folder / f".{layout}-plugin").mkdir(parents=True)
    (folder / f".{layout}-plugin" / "plugin.json").write_text(json.dumps(fields), encoding="utf-8")
    return folder


def test_a_claude_bundle_is_counted_not_run(tmp_path: Path) -> None:
    folder = bundle(tmp_path, {"name": "kit", "description": "Tools", "version": "1.2.0"})
    for part in ("skills/a", "skills/b", "commands", "agents", "hooks"):
        (folder / part).mkdir(parents=True)
    (folder / "skills" / "a" / "SKILL.md").write_text("x", encoding="utf-8")
    (folder / "skills" / "b" / "SKILL.md").write_text("x", encoding="utf-8")
    (folder / "commands" / "go.md").write_text("x", encoding="utf-8")
    (folder / "agents" / "helper.md").write_text("x", encoding="utf-8")
    (folder / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
    servers = {"mcpServers": {"one": {"command": "x"}, "two": {"url": "https://x.test"}}}
    (folder / ".mcp.json").write_text(json.dumps(servers), encoding="utf-8")
    found = read_bundle(folder)
    assert (found.id, found.layout, found.version, found.description) == (
        "kit",
        Layout.CLAUDE,
        "1.2.0",
        "Tools",
    )
    assert (found.skills, found.commands, found.agents, found.connectors, found.hooks) == (2, 1, 1, 2, True)


@pytest.mark.parametrize("layout", ["codex", "cursor"])
def test_codex_and_cursor_bundles_are_read_too(tmp_path: Path, layout: str) -> None:
    folder = bundle(tmp_path, {"name": "kit", "skills": {"paths": ["./more"]}}, layout)
    (folder / "more" / "x").mkdir(parents=True)
    (folder / "more" / "x" / "SKILL.md").write_text("x", encoding="utf-8")
    found = read_bundle(folder)
    assert (found.layout.value, found.skills, found.connectors, found.hooks) == (layout, 1, 0, False)


def test_inline_servers_and_declared_paths_are_counted(tmp_path: Path) -> None:
    fields = {"name": "kit", "mcpServers": {"a": {"command": "x"}}, "commands": "./cmds", "hooks": "./h.json"}
    folder = bundle(tmp_path, fields)
    (folder / "cmds").mkdir()
    (folder / "cmds" / "a.md").write_text("x", encoding="utf-8")
    found = read_bundle(folder)
    assert (found.connectors, found.commands, found.hooks) == (1, 1, True)


def test_a_server_file_named_by_the_manifest_is_read(tmp_path: Path) -> None:
    folder = bundle(tmp_path, {"name": "kit", "mcpServers": "./servers.json"})
    (folder / "servers.json").write_text(json.dumps({"mcpServers": {"a": {}}}), encoding="utf-8")
    assert read_bundle(folder).connectors == 1
    (folder / "servers.json").write_text(json.dumps({"mcpServers": ["a"]}), encoding="utf-8")
    assert read_bundle(folder).connectors == 0


@pytest.mark.parametrize(
    "path", ["../outside", "/etc", "skills", ".\\skills", "./a/../../outside", "./a/../b"]
)
def test_a_path_leading_outside_the_bundle_is_refused(tmp_path: Path, path: str) -> None:
    folder = bundle(tmp_path, {"name": "kit", "skills": path})
    assert refused(lambda: read_bundle(folder)).code == "registry.path_escape"


def test_a_hooks_path_leading_outside_is_refused(tmp_path: Path) -> None:
    folder = bundle(tmp_path, {"name": "kit", "hooks": "../evil.json"})
    assert refused(lambda: read_bundle(folder)).code == "registry.path_escape"


def test_plain_text_secrets_in_the_server_config_refuse_the_bundle(tmp_path: Path) -> None:
    folder = bundle(tmp_path, {"name": "kit"})
    servers = {"mcpServers": {"gh": {"command": "x", "env": {"GITHUB_TOKEN": "ghp_" + "a" * 36}}}}
    (folder / ".mcp.json").write_text(json.dumps(servers), encoding="utf-8")
    assert refused(lambda: read_bundle(folder)).code == "registry.secret_in_manifest"


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"description": "no name"}, "registry.bad_name"),
        ({"name": "Kit"}, "registry.bad_name"),
        ({"name": "kit", "description": 5}, "registry.bad_manifest"),
        ({"name": "kit", "version": "x" * 41}, "registry.bad_manifest"),
    ],
)
def test_a_bad_bundle_manifest_is_refused(tmp_path: Path, fields: dict[str, object], code: str) -> None:
    assert refused(lambda: read_bundle(bundle(tmp_path, fields))).code == code


def test_a_folder_that_is_not_a_bundle_is_refused(tmp_path: Path) -> None:
    assert refused(lambda: read_bundle(tmp_path)).code == "registry.no_manifest"


def test_a_named_server_file_that_does_not_exist_counts_as_none(tmp_path: Path) -> None:
    assert read_bundle(bundle(tmp_path, {"name": "kit", "mcpServers": "./missing.json"})).connectors == 0


def test_a_safe_looking_path_that_is_a_link_outside_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = bundle(tmp_path, {"name": "kit", "skills": "./link"})
    real_resolve = Path.resolve

    def resolve(self: Path, strict: bool = False) -> Path:
        resolved = real_resolve(self, strict=strict)
        return tmp_path / "outside" if resolved.name == "link" else resolved

    monkeypatch.setattr(Path, "resolve", resolve)
    assert refused(lambda: read_bundle(folder)).code == "registry.path_escape"


def test_inline_hooks_count_as_hooks(tmp_path: Path) -> None:
    assert read_bundle(bundle(tmp_path, {"name": "kit", "hooks": {"Stop": []}})).hooks is True

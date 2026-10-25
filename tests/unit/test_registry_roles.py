"""The role library: role packs from a local folder, checked before any becomes an agent (F17 slice 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from yaqzan.core.registry.roles import LINE_LIMITS, load_roles, version_of
from yaqzan.core.sdk import ModelLevel

CARD = """# Monitoring

| | |
|---|---|
| **Held by** | AI agent |
| **Highest permission** | {permission} |

## Mission
Know within 60 seconds when anything is down.
"""


def pack(root: Path, name: str, permission: str = "Read", **extra: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "ROLE.md").write_text(CARD.format(permission=permission), encoding="utf-8")
    (folder / "authority.md").write_text("# Authority\n\nNever restart anything.\n", encoding="utf-8")
    for (
        file,
        text,
    ) in extra.items():  # kpis_md -> kpis.md, instructions__outreach_md -> instructions/outreach.md
        path = folder / (file.replace("__", "/").removesuffix("_md") + ".md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return folder


def test_a_good_pack_loads_with_its_title_mission_permission_and_version(tmp_path: Path) -> None:
    pack(tmp_path, "monitoring", kpis_md="")
    library = load_roles(tmp_path)
    assert library.problems == ()
    (role,) = library.packs
    assert (role.id, role.title, role.permission) == ("monitoring", "Monitoring", "read")
    assert role.mission == "Know within 60 seconds when anything is down."
    assert role.version == version_of(role.files)
    assert library.get("monitoring") == role
    assert library.get("sales") is None


@pytest.mark.parametrize(
    ("words", "level"), [("Read (writes findings only)", "read"), ("Draft", "draft"), ("Act", "act")]
)
def test_the_first_word_of_the_highest_permission_decides_it(tmp_path: Path, words: str, level: str) -> None:
    pack(tmp_path, "a-role", permission=words)
    assert load_roles(tmp_path).packs[0].permission == level


@pytest.mark.parametrize(
    "words", ["Board member: every authority except overruling the Chairman", "Everything"]
)
def test_the_boards_own_packs_are_people_not_agents(tmp_path: Path, words: str) -> None:
    pack(tmp_path, "ceo", permission=words)
    library = load_roles(tmp_path)
    assert library.packs == ()
    assert [p.code for p in library.problems] == ["role.not_an_agent"]


def test_a_changed_pack_has_a_new_version_and_an_unchanged_one_the_same(tmp_path: Path) -> None:
    folder = pack(tmp_path, "monitoring")
    first = load_roles(tmp_path).packs[0].version
    assert load_roles(tmp_path).packs[0].version == first
    (folder / "authority.md").write_text(
        "# Authority\n\nNever restart anything. Never deploy.\n", encoding="utf-8"
    )
    assert load_roles(tmp_path).packs[0].version != first


def test_shared_rule_folders_and_loose_files_are_not_roles(tmp_path: Path) -> None:
    pack(tmp_path, "monitoring")
    (tmp_path / "_shared").mkdir()
    (tmp_path / "_shared" / "company-rules.md").write_text("# Rules\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# HR\n", encoding="utf-8")
    assert [p.id for p in load_roles(tmp_path).packs] == ["monitoring"]


def test_sub_folders_are_part_of_the_pack(tmp_path: Path) -> None:
    pack(tmp_path, "sales", instructions__outreach_md="# Outreach\n")
    files = load_roles(tmp_path).packs[0].files
    assert "instructions/outreach.md" in files


@pytest.mark.parametrize(
    ("make", "code"),
    [
        (lambda root: (pack(root, "a-role") / "authority.md").unlink(), "role.incomplete"),
        (
            lambda root: (pack(root, "a-role") / "notes.txt").write_text("x", encoding="utf-8"),
            "role.not_markdown",
        ),
        (
            lambda root: (pack(root, "a-role") / "kpis.md").write_text(
                "x\n" * (LINE_LIMITS["kpis.md"] + 1), encoding="utf-8"
            ),
            "role.too_long",
        ),
        (lambda root: pack(root, "Bad_Name"), "role.bad_name"),
        (
            lambda root: (pack(root, "a-role") / "ROLE.md").write_bytes(b"\xff\xfe not utf-8"),
            "registry.not_utf8",
        ),
    ],
)
def test_a_broken_pack_is_refused_and_says_why(tmp_path: Path, make: object, code: str) -> None:
    make(tmp_path)  # type: ignore[operator]
    library = load_roles(tmp_path)
    assert library.packs == ()
    assert [p.code for p in library.problems] == [code]


def test_a_missing_library_folder_is_a_problem_not_a_crash(tmp_path: Path) -> None:
    library = load_roles(tmp_path / "nowhere")
    assert library.packs == ()
    assert [p.code for p in library.problems] == ["role.no_library"]


def test_a_pack_carries_the_model_level_its_card_names(tmp_path: Path) -> None:
    folder = pack(tmp_path, "head-of-engineering")
    card = (folder / "ROLE.md").read_text(encoding="utf-8")
    (folder / "ROLE.md").write_text(
        card.replace("| **Held by** |", "| **Model** | Claude Opus 5.5, medium effort |\n| **Held by** |"),
        encoding="utf-8",
    )
    (role,) = load_roles(tmp_path).packs
    assert role.level is ModelLevel.OPUS


def test_a_pack_whose_card_names_no_model_gets_level_0(tmp_path: Path) -> None:
    pack(tmp_path, "monitoring")
    (role,) = load_roles(tmp_path).packs
    assert role.level is ModelLevel.CODE

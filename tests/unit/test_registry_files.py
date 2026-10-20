from pathlib import Path

import pytest

from yaqzan.core.registry import files
from yaqzan.core.registry.files import (
    ItemError,
    fingerprint,
    lenient_mapping,
    load_mapping,
    read_text,
    refuse_secrets,
    split_frontmatter,
)


def code_of(call: object) -> str:
    assert callable(call)
    with pytest.raises(ItemError) as caught:
        call()
    return caught.value.code


# ---------------------------------------------------------------- reading text


def test_a_bom_and_windows_line_endings_are_accepted(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_bytes(b"\xef\xbb\xbf---\r\nname: a\r\n---\r\nbody\r")
    assert read_text(path) == "---\nname: a\n---\nbody\n"


def test_a_file_too_big_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "big.yaml"
    path.write_bytes(b"a" * (files.MAX_FILE_BYTES + 1))
    assert code_of(lambda: read_text(path)) == "registry.too_big"


def test_a_file_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "x.yaml"
    path.write_bytes(b"name: \xff\xfe")
    assert code_of(lambda: read_text(path)) == "registry.not_utf8"


def test_a_linked_file_is_not_followed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "x.yaml"
    path.write_text("a: 1", encoding="utf-8")
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "x.yaml")
    assert code_of(lambda: read_text(path)) == "registry.symlink"


# ---------------------------------------------------------------- YAML


def test_a_mapping_is_read() -> None:
    assert load_mapping("a: 1\nb: [x, y]\n", "f") == {"a": 1, "b": ["x", "y"]}


@pytest.mark.parametrize(
    ("text", "code"), [("a: [1", "registry.bad_yaml"), ("- a\n- b", "registry.not_a_mapping")]
)
def test_bad_or_wrong_shaped_yaml_is_refused(text: str, code: str) -> None:
    assert code_of(lambda: load_mapping(text, "f")) == code


def test_yaml_tags_that_build_objects_are_refused() -> None:
    assert (
        code_of(lambda: load_mapping("a: !!python/object/apply:os.system ['echo hi']", "f"))
        == "registry.bad_yaml"
    )


def test_an_alias_bomb_is_refused() -> None:
    bomb = "a: &a [x, x, x, x, x, x, x, x, x, x]\n"
    for level in "bcdefgh":
        previous = chr(ord(level) - 1)
        bomb += f"{level}: &{level} [*{previous}, *{previous}, *{previous}, *{previous}, *{previous}]\n"
    assert code_of(lambda: load_mapping(bomb, "f")) == "registry.too_complex"


def test_very_deep_nesting_is_refused() -> None:
    assert code_of(lambda: load_mapping("a: " + "[" * 30 + "]" * 30, "f")) == "registry.too_complex"


def test_an_unquoted_colon_in_a_value_is_retried_quoted() -> None:
    text = 'name: x\ndescription: Build for AWS: ECS and "more"\nmetadata:\n  note: a: b\n'
    assert lenient_mapping(text, "f") == {
        "name": "x",
        "description": 'Build for AWS: ECS and "more"',
        "metadata": {"note": "a: b"},
    }


def test_the_retry_does_not_hide_other_problems() -> None:
    assert code_of(lambda: lenient_mapping("- a: b: c", "f")) == "registry.bad_yaml"
    assert code_of(lambda: lenient_mapping("[1, 2]", "f")) == "registry.not_a_mapping"


# ---------------------------------------------------------------- front matter


def test_the_header_and_body_are_split() -> None:
    assert split_frontmatter("---\nname: a\n---\n# Body\n---- not a fence\n", "f") == (
        "name: a\n",
        "# Body\n---- not a fence\n",
    )


def test_a_header_closed_at_the_end_of_the_file_is_fine() -> None:
    assert split_frontmatter("---\nname: a\n---", "f") == ("name: a\n", "")


@pytest.mark.parametrize("text", ["name: a\n", "---\nname: a\n", "---\nname: a\n----\nbody"])
def test_a_missing_header_fence_is_refused(text: str) -> None:
    assert code_of(lambda: split_frontmatter(text, "f")) == "registry.no_frontmatter"


# ---------------------------------------------------------------- secrets


@pytest.mark.parametrize(
    "data",
    [
        {"description": "use ghp_" + "a" * 36},
        {"env": {"GITHUB_TOKEN": "abc123"}},
        {"servers": [{"url": "https://user:hunter2@example.com/x"}]},
        {"api_key": "plain"},
        {"auth": {"header": "Bearer abcdefghijklmnop"}},
        {"headers": {"Authorization": "Bearer ${KEY} sk-live-extra"}},
    ],
)
def test_anything_that_looks_like_a_secret_is_refused(data: dict[str, object]) -> None:
    assert code_of(lambda: refuse_secrets(data, "f")) == "registry.secret_in_manifest"


def test_the_refusal_does_not_repeat_the_secret() -> None:
    with pytest.raises(ItemError) as caught:
        refuse_secrets({"api_key": "s3cr3t-value"}, "f")
    assert "s3cr3t" not in str(caught.value)


@pytest.mark.parametrize(
    "data",
    [
        {"env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}},
        {"env": {"API_KEY": "${env:API_KEY}"}},
        {"headers": {"Authorization": "Bearer ${PADDLE_API_KEY}"}},
        {"author": {"name": "A", "email": "a@example.com"}},
        {"credentials": [{"name": "token", "scope_hint": "read-only: issues", "verify": True}]},
        {"token": ""},
    ],
)
def test_slot_names_placeholders_and_emails_are_fine(data: dict[str, object]) -> None:
    refuse_secrets(data, "f")


# ---------------------------------------------------------------- fingerprints


def make_folder(root: Path) -> Path:
    (root / "sub").mkdir(parents=True)
    (root / "SKILL.md").write_text("a", encoding="utf-8")
    (root / "sub" / "x.py").write_text("b", encoding="utf-8")
    return root


def test_a_fingerprint_is_stable_and_ignores_caches(tmp_path: Path) -> None:
    folder = make_folder(tmp_path / "a")
    first = fingerprint(folder)
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\0")
    (folder / "sub" / "y.pyc").write_bytes(b"\0")
    assert fingerprint(folder) == first
    assert fingerprint(make_folder(tmp_path / "b")) == first


@pytest.mark.parametrize("change", ["edit", "rename", "add"])
def test_any_change_changes_the_fingerprint(tmp_path: Path, change: str) -> None:
    folder = make_folder(tmp_path / "a")
    before = fingerprint(folder)
    if change == "edit":
        (folder / "SKILL.md").write_text("A", encoding="utf-8")
    elif change == "rename":
        (folder / "sub" / "x.py").rename(folder / "sub" / "z.py")
    else:
        (folder / "new.md").write_text("", encoding="utf-8")
    assert fingerprint(folder) != before


def test_moving_bytes_between_files_changes_the_fingerprint(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (one / "a").write_text("xy", encoding="utf-8")
    (one / "b").write_text("", encoding="utf-8")
    (two / "a").write_text("x", encoding="utf-8")
    (two / "b").write_text("y", encoding="utf-8")
    assert fingerprint(one) != fingerprint(two)


def test_too_many_files_or_bytes_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = make_folder(tmp_path / "a")
    monkeypatch.setattr(files, "MAX_ITEM_FILES", 1)
    assert code_of(lambda: fingerprint(folder)) == "registry.too_many_files"
    monkeypatch.setattr(files, "MAX_ITEM_FILES", 10)
    monkeypatch.setattr(files, "MAX_ITEM_BYTES", 1)
    assert code_of(lambda: fingerprint(folder)) == "registry.too_big"


def test_a_link_inside_an_item_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = make_folder(tmp_path / "a")
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "sub")
    assert code_of(lambda: fingerprint(folder)) == "registry.symlink"

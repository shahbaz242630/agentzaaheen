"""The vault's master key tool and the shape checks for the Claude key and the server token. No database."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentzaaheen.core.policy import key_problem, token_problem
from agentzaaheen.core.policy.claude_key import MAX_KEY_CHARS
from agentzaaheen.core.policy.server_token import MAX_PART_CHARS
from tools import vault_key


def test_make_key_makes_the_file_once_and_never_overwrites_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key_file = tmp_path / "keys" / "vault.key"
    assert vault_key.main(["make-key"], key_file=key_file) == 0
    first = key_file.read_text(encoding="ascii")
    assert "OneDrive Personal Vault" in capsys.readouterr().out
    assert vault_key.main(["make-key"], key_file=key_file) == 0
    assert "already at" in capsys.readouterr().out
    assert key_file.read_text(encoding="ascii") == first
    ring = vault_key.load_keyring(key_file)
    assert ring is not None
    assert ring.current_id == vault_key.KEY_ID


def test_no_key_file_or_a_bad_one_gives_no_keyring_and_says_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert vault_key.load_keyring(tmp_path / "missing.key") is None
    assert "Make one: python -m tools.vault_key make-key" in capsys.readouterr().out
    bad = tmp_path / "bad.key"
    bad.write_text("not a key\n", encoding="ascii")
    if os.name == "posix":  # a key file must be private there, or it is refused before it is read
        bad.chmod(0o600)
    assert vault_key.load_keyring(bad) is None
    assert "could not be used (vault.bad_key_file)" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("", "it is empty"),
        ("sk-ant-" + "x" * MAX_KEY_CHARS, "it is too long"),
        ("sk-ant- api03", "it contains spaces or control characters"),
        ("sk-ant-api03\x00", "it contains spaces or control characters"),
        ("sk-ant-é", "it contains spaces or control characters"),
        ("sk-proj-abc", "Claude API keys start with sk-ant-"),
        ("sk-ant-api03-abc", None),
    ],
)
def test_the_key_shape_check(raw: str, why: str | None) -> None:
    assert key_problem(raw) == why


ID = "0" * 32 + ".access"  # built here, never a real token
SECRET = "f" * 64


@pytest.mark.parametrize(
    ("client_id", "secret", "why"),
    [
        (ID, SECRET, None),
        ("", SECRET, "a part is empty"),
        (ID, "", "a part is empty"),
        (ID, "f" * (MAX_PART_CHARS + 1), "a part is too long"),
        ("x" * (MAX_PART_CHARS - len(".access")) + ".access", SECRET, None),  # exactly at the limit
        (ID, SECRET[:10] + " " + SECRET[11:], "a part contains spaces or other characters a token never has"),
        (
            ID,
            SECRET[:10] + "\n" + SECRET[11:],
            "a part contains spaces or other characters a token never has",
        ),
        (ID + "é", SECRET, "a part contains spaces or other characters a token never has"),
        ("0" * 32, SECRET, "the Client ID ends with .access"),
        (".access", SECRET, "the Client ID ends with .access"),
        (ID, ID, "the two parts look swapped"),
    ],
)
def test_the_server_token_shape_check(client_id: str, secret: str, why: str | None) -> None:
    assert token_problem(client_id, secret) == why

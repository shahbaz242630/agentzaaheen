"""The name-prints tool without a database: the key file, and every way it refuses before saving (F10 2b).

No real network, no real key: Paddle is the fake ``PaddleWorld``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from agentzaaheen.core.vault import load_master_key
from tests.fixtures import paddle_world as world
from tools import name_prints

FAKE_KEY = "pdl_live_apikey_" + "z" * 20  # built here, never a real key


def test_the_key_is_made_once_and_never_overwritten(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key_file = tmp_path / "keys" / "names.key"
    assert name_prints.main(["make-key"], key_file=key_file) == 0
    first = key_file.read_bytes()
    assert load_master_key(key_file).key_id == name_prints.KEY_ID
    assert name_prints.main(["make-key"], key_file=key_file) == 0
    assert key_file.read_bytes() == first
    out = capsys.readouterr().out
    assert "made the fingerprint key" in out
    assert "already" in out


def test_without_a_key_file_nothing_is_read(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["paddle", "--workspace", "zaaheen", "--key-env", "K"]
    assert name_prints.main(args, environ={"K": FAKE_KEY}, key_file=tmp_path / "none.key") == 1
    assert "Make one" in capsys.readouterr().out


def test_without_the_paddle_key_nothing_is_read(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    key_file = tmp_path / "names.key"
    name_prints.make_key(key_file)
    assert (
        name_prints.main(
            ["paddle", "--workspace", "zaaheen", "--key-env", "K"], environ={}, key_file=key_file
        )
        == 1
    )
    assert "could not run" in capsys.readouterr().out


def test_without_the_laptop_database_it_says_how_to_start_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def not_running() -> None:
        return None

    monkeypatch.setattr(name_prints, "prepare", not_running)
    key_file = tmp_path / "names.key"
    name_prints.make_key(key_file)
    args = ["paddle", "--workspace", "zaaheen", "--key-env", "K"]
    assert name_prints.main(args, environ={"K": FAKE_KEY}, key_file=key_file) == 1
    assert "laptop_db up" in capsys.readouterr().out


def test_paddle_names_are_read_from_the_live_host_only() -> None:
    paddle = world.PaddleWorld()
    names = asyncio.run(name_prints.read_paddle_names(SecretStr(FAKE_KEY), httpx.MockTransport(paddle.serve)))
    assert sorted(names) == ["Fatima Al Mansoori", "Grace", "Zoë O'Brien"]
    assert {r.url.host for r in paddle.requests} == {"api.paddle.com"}

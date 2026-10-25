"""The live Claude check with the key sealed in a real database and a fake Claude (F10 slice 2c).

The key is opened from the vault for the one call, never printed, and the call's
cost lands in the spend book in the database, so the monthly caps count it.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from agentzaaheen.core.store import ModelCalls
from agentzaaheen.core.vault import Keyring, load_master_key
from tests.integration.conftest import ScratchDb, add_workspaces, open_db, run
from tools import claude_check, vault_key
from tools.laptop_db import LAPTOP_TENANT
from tools.name_prints import make_key

FAKE_KEY = "sk-ant-api03-" + "s" * 40  # built here, never a real key
MONTH = ("2026-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00")


def answered(request: httpx.Request) -> httpx.Response:
    assert request.headers["x-api-key"] == FAKE_KEY  # the sealed key, opened for this call
    return httpx.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": json.loads(request.content)["model"],
            "content": [{"type": "text", "text": "The connection to Claude works."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 40, "output_tokens": 9},
        },
    )


def test_the_sealed_key_is_used_and_the_cost_is_counted(
    test_db: ScratchDb, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(add_workspaces(test_db.app_url, LAPTOP_TENANT, vault_key.CLAUDE_WORKSPACE))
    key_file = tmp_path / "vault.key"
    assert make_key(key_file, vault_key.KEY_ID)
    master = load_master_key(key_file)

    async def seal() -> None:
        async with open_db(test_db.app_url) as db:
            await vault_key.claude_slot(db, Keyring([master], current_id=master.key_id)).save(
                SecretStr(FAKE_KEY)
            )

    run(seal())

    async def the_test_db() -> SecretStr:
        return SecretStr(test_db.app_url)

    code = claude_check.main(
        [], key_file=key_file, prepare=the_test_db, transport=httpx.MockTransport(answered)
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "claude-sonnet-5-5 answered." in out
    assert "(added to this month's spend)" in out
    assert FAKE_KEY not in out

    async def spent() -> int:
        start, end = (datetime.fromisoformat(t) for t in MONTH)
        async with open_db(test_db.app_url) as db:
            return await ModelCalls(db.scoped(LAPTOP_TENANT)).spent("code-review", start=start, end=end)

    assert run(spent()) == 170  # 40 x 2 + 9 x 10 millionths of a USD


def test_with_no_key_saved_nothing_is_sent(
    test_db: ScratchDb, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(add_workspaces(test_db.app_url, LAPTOP_TENANT, vault_key.CLAUDE_WORKSPACE))
    key_file = tmp_path / "vault.key"
    assert make_key(key_file, vault_key.KEY_ID)

    async def the_test_db() -> SecretStr:
        return SecretStr(test_db.app_url)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent")

    code = claude_check.main(
        [], key_file=key_file, prepare=the_test_db, transport=httpx.MockTransport(refuse)
    )
    assert code == 1
    assert "no Claude key is saved" in capsys.readouterr().out

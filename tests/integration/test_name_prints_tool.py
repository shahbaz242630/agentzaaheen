"""The name-prints tool end to end against a real Postgres and a fake Paddle (F10 slice 2b).

It reads the names, saves only fingerprints in the chosen workspace, prints
counts only (never a name, never the key), and a second read adds nothing.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from agentzaaheen.core.llm_gateway import prints_of
from agentzaaheen.core.store import AllNamePrints
from agentzaaheen.core.vault import NamePrinter, load_master_key
from tests.fixtures import paddle_world as world
from tests.integration.conftest import ScratchDb, add_workspaces, open_db, run
from tools import name_prints
from tools.laptop_db import LAPTOP_TENANT, LAPTOP_WORKSPACES

FAKE_KEY = "pdl_live_apikey_" + "z" * 20  # built here, never a real key
ZAAHEEN = next(w.workspace_id for w in LAPTOP_WORKSPACES if w.slug == "zaaheen")
ARGS = ["paddle", "--workspace", "zaaheen", "--key-env", "K"]


def test_names_are_read_and_only_their_fingerprints_saved(
    test_db: ScratchDb, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(add_workspaces(test_db.app_url, LAPTOP_TENANT, ZAAHEEN))

    async def the_test_db() -> SecretStr:
        return SecretStr(test_db.app_url)

    monkeypatch.setattr(name_prints, "prepare", the_test_db)
    key_file = tmp_path / "names.key"
    name_prints.make_key(key_file)
    printer = NamePrinter(load_master_key(key_file))
    paddle = world.PaddleWorld()

    def read() -> int:
        transport = httpx.MockTransport(paddle.serve)
        return name_prints.main(ARGS, environ={"K": FAKE_KEY}, key_file=key_file, transport=transport)

    assert read() == 0
    out = capsys.readouterr().out
    assert "zaaheen: 3 customer names read, 5 fingerprints, 5 new. No name was kept." in out
    for secret in ("Fatima", "Grace", "Zo", FAKE_KEY):
        assert secret not in out

    async def stored() -> set[tuple[str, bytes]]:
        async with open_db(test_db.app_url) as db:
            return await AllNamePrints(db.scoped(LAPTOP_TENANT)).made_with(name_prints.KEY_ID)

    expected = {
        (p.kind.value, p.print) for p in prints_of(["Fatima Al Mansoori", "Grace", "Zoë O'Brien"], printer)
    }
    assert run(stored()) == expected
    assert read() == 0
    assert "5 fingerprints, 0 new" in capsys.readouterr().out

"""Agent Zaaheen on the server (F17 slice 7): secrets from systemd credentials, phone off unless --send."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from tools import laptop_db, server_core
from tools.server_core import NotReadyError, admin_url, credentials, plan

ODD = "a@b/c:1"  # made up here: characters a database address must quote
KEYS = {
    "postgres_password": ODD + "\n",
    "vault_key": "vault-1:" + "a" * 44,
    "PADDLE_LIVE_API_KEY_ZAAHEEN": "pdl_live_x",
    "GITHUB_READ_TOKEN_AGENTZAAHEEN": "github_pat_x",
    "SUPABASE_READ_TOKEN_SANDOQ": "sbp_x",
    "AGENTZAAHEEN_NTFY_TOPIC": "topic-x",
    "AGENTZAAHEEN_BRIEFING_PING_URL": "https://hc-ping.com/x",
    "CLOUDFLARE_D1_READ_TOKEN_AGENTZAAHEEN": "d1_x",
}
CHECKER_DB = "a" * 32 + "/536927ad-6da6-44c8-a562-343c6285155d"  # made up here


@pytest.fixture
def creds(tmp_path: Path) -> Path:
    folder = tmp_path / "creds"
    folder.mkdir()
    for name, value in KEYS.items():
        (folder / name).write_text(value, encoding="utf-8")
    return folder


@pytest.fixture
def config(tmp_path: Path) -> Path:
    folder = tmp_path / "config"
    folder.mkdir()
    (folder / "uptime.yaml").write_text("{}", encoding="utf-8")
    (folder / "supabase.yaml").write_text("{}", encoding="utf-8")
    (folder / "keys.yaml").write_text("[]", encoding="utf-8")
    (folder / "slo.yaml").write_text("", encoding="utf-8")
    (folder / "checker-db.txt").write_text(CHECKER_DB + "\n", encoding="utf-8")
    return folder


def options_of(argv: list[str]) -> dict[str, str]:
    return {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i].startswith("--")}


def test_credentials_are_read_by_name_and_trimmed(creds: Path) -> None:
    read = credentials(creds)
    assert read["postgres_password"] == ODD
    assert set(read) == set(KEYS)


def test_no_credentials_folder_is_said_plainly(tmp_path: Path) -> None:
    with pytest.raises(NotReadyError, match="no credentials folder"):
        credentials(tmp_path / "missing")


def test_the_admin_address_quotes_the_password() -> None:
    assert admin_url(ODD) == "postgresql://postgres:a%40b%2Fc%3A1@127.0.0.1:5432/postgres"


def test_without_send_nothing_reaches_the_phone(creds: Path, config: Path) -> None:
    argv, env = plan(credentials(creds), creds / "vault_key", config, send=False)
    options = options_of(argv)
    assert "--ntfy-topic-env" not in options
    assert "--briefing-ping-env" not in options
    assert options["--paddle-key-env"] == "PADDLE_LIVE_API_KEY_ZAAHEEN"
    assert options["--github-token-env"] == "GITHUB_READ_TOKEN_AGENTZAAHEEN"
    assert options["--uptime"] == str(config / "uptime.yaml")
    assert options["--supabase"] == str(config / "supabase.yaml")
    assert options["--keys"] == str(config / "keys.yaml")
    assert options["--slo"] == str(config / "slo.yaml")  # the uptime targets, as on the laptop
    assert options["--checker-db-key-env"] == "CLOUDFLARE_D1_READ_TOKEN_AGENTZAAHEEN"
    assert options["--checker-db"] == CHECKER_DB
    assert options["--vault-key"] == str(creds / "vault_key")
    assert "--switches" in argv
    assert "--model-url" not in options  # the summaries need no model
    assert env["SUPABASE_READ_TOKEN_SANDOQ"] == "sbp_x"
    assert "postgres_password" not in env  # only key-shaped names become environment
    assert "vault_key" not in env


def test_with_send_the_topic_and_the_ping_are_used(creds: Path, config: Path) -> None:
    options = options_of(plan(credentials(creds), creds / "vault_key", config, send=True)[0])
    assert options["--ntfy-topic-env"] == "AGENTZAAHEEN_NTFY_TOPIC"
    assert options["--briefing-ping-env"] == "AGENTZAAHEEN_BRIEFING_PING_URL"


def test_a_reader_without_its_key_or_file_is_left_out(creds: Path, tmp_path: Path) -> None:
    (creds / "PADDLE_LIVE_API_KEY_ZAAHEEN").unlink()
    options = options_of(plan(credentials(creds), creds / "vault_key", tmp_path / "no-config", send=True)[0])
    assert "--paddle-key-env" not in options
    assert "--uptime" not in options
    assert "--supabase" not in options
    assert "--keys" not in options
    assert "--slo" not in options


@pytest.mark.parametrize("missing", ["key", "slo.yaml", "checker-db.txt"])
def test_the_uptime_targets_need_their_key_list_and_database(creds: Path, config: Path, missing: str) -> None:
    (creds / "CLOUDFLARE_D1_READ_TOKEN_AGENTZAAHEEN" if missing == "key" else config / missing).unlink()
    options = options_of(plan(credentials(creds), creds / "vault_key", config, send=False)[0])
    assert not {"--slo", "--checker-db", "--checker-db-key-env"} & options.keys()


@pytest.mark.parametrize("missing", ["postgres_password", "vault_key"])
def test_the_database_password_and_vault_key_are_required(creds: Path, config: Path, missing: str) -> None:
    (creds / missing).write_text("  \n", encoding="utf-8")
    with pytest.raises(NotReadyError, match=missing):
        plan(credentials(creds), creds / "vault_key", config, send=False)


def test_main_outside_systemd_refuses(capsys: pytest.CaptureFixture[str]) -> None:
    started: list[object] = []

    def start(*args: object, **kw: object) -> int:
        started.append(args)
        return 0

    assert server_core.main([], environ={}, start=start) == 1
    assert started == []
    assert "run this only as the systemd service" in capsys.readouterr().out


def test_main_says_what_is_missing_and_never_a_secret(
    creds: Path, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (creds / "vault_key").unlink()
    code = server_core.main(
        ["--config", str(config)], environ={"CREDENTIALS_DIRECTORY": str(creds)}, start=lambda *a, **k: 0
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "vault_key is missing" in out
    assert ODD not in out


def test_main_starts_the_dashboard_with_the_server_database_and_a_page(
    creds: Path, config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}
    set_up_with: list[str] = []

    async def fake_set_up(admin: str) -> SecretStr:
        set_up_with.append(admin)
        return SecretStr("postgresql://app@127.0.0.1/agentzaaheen")

    monkeypatch.setattr(laptop_db, "set_up", fake_set_up)

    def start(argv: list[str], **kw: Any) -> int:
        seen.update(argv=argv, **kw)
        seen["page"] = (kw["static_dir"] / "index.html").read_text(encoding="utf-8")
        vault = Path(argv[argv.index("--vault-key") + 1])
        seen["vault"] = vault.read_text(encoding="ascii").strip()
        seen["vault_mode"] = vault.stat().st_mode & 0o777
        seen["url"] = asyncio.run(kw["prepare"]())
        return 0

    code = server_core.main(
        ["--send", "--config", str(config)], environ={"CREDENTIALS_DIRECTORY": str(creds)}, start=start
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "--ntfy-topic-env" in seen["argv"]
    assert seen["environ"]["PADDLE_LIVE_API_KEY_ZAAHEEN"] == "pdl_live_x"
    assert "Agent Zaaheen is running on the server" in seen["page"]
    assert seen["vault"] == KEYS["vault_key"]  # a private copy, not systemd's root-owned file
    assert str(creds) not in " ".join(seen["argv"])
    if os.name == "posix":
        assert seen["vault_mode"] == 0o600
    assert not seen["static_dir"].exists()  # the page folder is removed after
    assert set_up_with == [admin_url(ODD)]
    assert "phone on" in out
    assert not any(value.strip() in out for value in KEYS.values())


def test_a_folder_among_the_credentials_is_not_read_as_one(creds: Path) -> None:
    (creds / "nested").mkdir()
    assert "nested" not in credentials(creds)

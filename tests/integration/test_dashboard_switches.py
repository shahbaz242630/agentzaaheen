"""The dashboard with ``--switches`` against a real database (ADR-030, F06 slice 2b).

The laptop database's set-up runs on the test server (in its own throwaway
database); then the dashboard is started for real, an item is switched off over
HTTP, and the switch is still off after a restart. A connector switched off is
not read at the next start.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agentzaaheen.core.api.agents import SwitchIn
from agentzaaheen.core.api.app import REQUEST_HEADER
from agentzaaheen.core.api.workspaces import WorkspaceDirectory
from agentzaaheen.core.auth import PasskeyError, Site
from agentzaaheen.core.policy import AgentVerdict, LifecycleVerdict
from agentzaaheen.core.store import (
    AuditRow,
    AuditRows,
    Database,
    DatabaseSettings,
    StoreError,
    StoreErrorCode,
)
from tests.fixtures.soft_authenticator import SoftAuthenticator
from tests.integration.conftest import Server, run
from tools import chat_tools, dashboard, dashboard_switches, laptop_db, vault_key

OURS = {REQUEST_HEADER: "1", "Origin": "http://127.0.0.1"}
SKILL = {"kind": "skill", "id": "triage-failing-build"}
GITHUB = {"kind": "connector", "id": "github"}


async def _admin(server: Server, *statements: str) -> None:
    conn = await asyncpg.connect(server.admin_url)
    try:
        for statement in statements:
            await conn.execute(statement)
    finally:
        await conn.close()


@pytest.fixture
def laptop_url(server: Server) -> Iterator[SecretStr]:
    # Named like the conftest's scratch databases, so an interrupted run is cleaned up next session.
    name = f"e_{uuid4().hex}"
    try:
        yield run(laptop_db.set_up(server.admin_url, name))
    finally:
        run(
            _admin(
                server,
                f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)',
                f"DROP ROLE IF EXISTS {laptop_db.APP}, {laptop_db.OWNER}",
            )
        )


@pytest.fixture
def ui(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return tmp_path


def start(ui: Path, url: SecretStr, argv: list[str] | None = None, **extra: object) -> FastAPI:
    apps: list[FastAPI] = []

    async def prepare() -> SecretStr:
        return url

    code = dashboard.main(
        ["--switches", *(argv or [])],
        static_dir=ui,
        serve=lambda app, _h, _p: apps.append(app),
        prepare=prepare,
        **extra,  # type: ignore[arg-type]
    )
    assert code == 0
    return apps[0]


def states(client: TestClient) -> dict[str, str]:
    page = client.get("/api/registry").json()
    return {item["id"]: item["state"] for group in ("connectors", "skills") for item in page[group]}


def test_set_up_twice_is_safe_and_the_dashboard_gets_least_privilege(
    laptop_url: SecretStr, server: Server
) -> None:
    again = run(laptop_db.set_up(server.admin_url, laptop_url.get_secret_value().rsplit("/", 1)[1]))
    assert again.get_secret_value() == laptop_url.get_secret_value()

    async def delete_switches() -> None:
        conn = await asyncpg.connect(laptop_url.get_secret_value())
        try:
            await conn.execute("DELETE FROM agentzaaheen.registry_switches")
        finally:
            await conn.close()

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        run(delete_switches())


def test_a_switch_turned_off_stays_off_after_a_restart(ui: Path, laptop_url: SecretStr) -> None:
    with TestClient(start(ui, laptop_url), base_url="http://127.0.0.1") as client:
        page = client.get("/api/registry").json()
        assert page["switches"] is True
        assert states(client) == {
            "github": "on",
            "paddle": "on",
            "supabase": "on",
            "uptime": "on",
            "check-slo": "on",
            "read-logs": "on",
            "trace-request": "on",
            "triage-failing-build": "on",
            "triage-outage": "on",
        }
        response = client.post("/api/registry/switch-off", json=SKILL, headers=OURS)
        assert response.status_code == 200
        assert {s["id"]: s["state"] for s in response.json()["skills"]} == {
            "check-slo": "on",
            "read-logs": "on",
            "trace-request": "on",
            "triage-failing-build": "off",  # only the one switched off
            "triage-outage": "on",
        }

    with TestClient(start(ui, laptop_url), base_url="http://127.0.0.1") as client:
        assert states(client)["triage-failing-build"] == "off"
        asked = client.post("/api/registry/requests", json=SKILL, headers=OURS)
        assert asked.status_code == 200
        (request,) = asked.json()["requests"]
        assert (request["item_id"], request["stale"]) == ("triage-failing-build", False)
        assert client.post("/api/registry/requests", json=SKILL, headers=OURS).json() == {
            "detail": "registry.already_asked"
        }
        withdrawn = client.post(f"/api/registry/requests/{request['id']}/withdraw", json={}, headers=OURS)
        assert withdrawn.json()["requests"] == []
        assert states(client)["triage-failing-build"] == "off"  # asking never switched it on


def test_a_connector_switched_off_is_not_read_at_the_next_start(
    ui: Path, laptop_url: SecretStr, capsys: pytest.CaptureFixture[str]
) -> None:
    with TestClient(start(ui, laptop_url), base_url="http://127.0.0.1") as client:
        assert client.post("/api/registry/switch-off", json=GITHUB, headers=OURS).status_code == 200
    capsys.readouterr()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"GitHub was called although switched off: {request.url}")

    start(
        ui,
        laptop_url,
        ["--github-token-env", "T"],
        environ={"T": "ghp_" + "x" * 36},
        transport=httpx.MockTransport(refuse),
    )
    assert "github is switched off on the Customize page, so it was not read." in capsys.readouterr().out


HERE = {REQUEST_HEADER: "1", "Origin": "http://localhost:8765"}


def test_set_up_a_passkey_then_approve_and_refuse_over_http(
    ui: Path, laptop_url: SecretStr, capsys: pytest.CaptureFixture[str]
) -> None:
    device = SoftAuthenticator()
    app = start(ui, laptop_url, ["--enrol-passkey"])
    out = capsys.readouterr().out
    code = out.split("The code to type on the Customize page is ")[1].split()[0]
    assert "open http://localhost:8765" in out
    with TestClient(app, base_url="http://localhost:8765") as client:
        assert client.get("/api/passkey").json() == {"enrolled": False, "enrolment_open": True}
        options = client.post("/api/passkey/enrol/options", json={"code": code}, headers=HERE).json()[
            "options"
        ]
        done = client.post(
            "/api/passkey/enrol", json={"code": code, "credential": device.register(options)}, headers=HERE
        )
        assert done.json() == {"enrolled": True, "enrolment_open": False}

        # Off, ask, approve with Windows Hello: on again.
        request = ask_after_switching_off(client)
        path = f"/api/registry/requests/{request}/approve"
        sign = client.post(f"{path}/options", json={}, headers=HERE).json()["options"]
        assert client.post(path, json={"credential": device.sign(sign)}, headers=HERE).status_code == 200
        assert states(client)["triage-failing-build"] == "on"

        # Off, ask, refuse: stays off, nothing waiting.
        request = ask_after_switching_off(client)
        refused = client.post(f"/api/registry/requests/{request}/refuse", json={}, headers=HERE)
        assert refused.json()["requests"] == []
        assert states(client)["triage-failing-build"] == "off"


def test_the_server_token_and_model_key_saved_with_windows_hello_go_with_each_call_to_our_server(
    ui: Path, laptop_url: SecretStr, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key_file = tmp_path / "vault.key"
    vault_key.main(["make-key"], key_file=key_file)
    seen: list[httpx.Request] = []

    def server(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Through the tunnel."}}]})

    device = SoftAuthenticator()
    argv = ["--enrol-passkey", "--vault-key", str(key_file), "--model-url", "https://llm.agentzaaheen.com"]
    app = start(ui, laptop_url, argv, transport=httpx.MockTransport(server))
    code = capsys.readouterr().out.split("The code to type on the Customize page is ")[1].split()[0]
    token = {"client_id": "0" * 32 + ".access", "client_secret": "f" * 64}  # built here, never a real one
    model_key = {"key": "a1" * 32}  # built here, never the real one
    question = {"model_id": chat_tools.LOCAL_MODEL_ID, "question": "hi"}
    with TestClient(app, base_url="http://localhost:8765") as client:
        before = client.post("/api/chat", json=question, headers=HERE)
        options = client.post("/api/passkey/enrol/options", json={"code": code}, headers=HERE).json()[
            "options"
        ]
        client.post(
            "/api/passkey/enrol", json={"code": code, "credential": device.register(options)}, headers=HERE
        )
        assert client.get("/api/server-token").json()["saved"] is False
        unsigned = client.post("/api/server-token", json=token, headers=HERE)
        sign = client.post("/api/server-token/options", json=token, headers=HERE).json()["options"]
        saved = client.post(
            "/api/server-token", json={**token, "credential": device.sign(sign)}, headers=HERE
        )
        token_only = client.post("/api/chat", json=question, headers=HERE)
        assert client.get("/api/model-key").json()["saved"] is False
        unsigned_key = client.post("/api/model-key", json=model_key, headers=HERE)
        sign = client.post("/api/model-key/options", json=model_key, headers=HERE).json()["options"]
        saved_key = client.post(
            "/api/model-key", json={**model_key, "credential": device.sign(sign)}, headers=HERE
        )
        after = client.post("/api/chat", json=question, headers=HERE)
        claude = client.get("/api/claude-key").json()
        still_token = client.get("/api/server-token").json()

    assert (before.status_code, before.json()) == (502, {"detail": "net.auth"})  # nothing saved: nothing sent
    assert unsigned.json() == {"detail": "server_token.needs_passkey"}
    assert saved.json()["saved"] is True
    assert token["client_secret"] not in saved.text
    assert (token_only.status_code, token_only.json()) == (502, {"detail": "net.auth"})  # no key, no call
    assert unsigned_key.json() == {"detail": "model_key.needs_passkey"}
    assert saved_key.json()["saved"] is True
    assert model_key["key"] not in saved_key.text
    assert after.json()["answer"] == "Through the tunnel."
    assert claude["saved"] is False  # their own slots: saving one never touches another
    assert still_token["saved"] is True
    (call,) = seen  # only the call with both was sent
    assert call.headers["CF-Access-Client-Id"] == token["client_id"]
    assert call.headers["CF-Access-Client-Secret"] == token["client_secret"]
    assert call.headers["Authorization"] == f"Bearer {model_key['key']}"


def ask_after_switching_off(client: TestClient) -> str:
    client.post("/api/registry/switch-off", json=SKILL, headers=HERE)
    (request,) = client.post("/api/registry/requests", json=SKILL, headers=HERE).json()["requests"]
    return str(request["id"])


def test_a_database_that_fails_at_start_is_explained(ui: Path, capsys: pytest.CaptureFixture[str]) -> None:
    async def broken() -> SecretStr:
        raise StoreError(StoreErrorCode.UNAVAILABLE, "down")

    assert dashboard.main(["--switches"], static_dir=ui, serve=lambda *_: None, prepare=broken) == 1
    assert "the laptop database could not be used (store.unavailable)" in capsys.readouterr().out


def test_enrol_passkey_needs_switches(ui: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert dashboard.main(["--enrol-passkey"], static_dir=ui, serve=lambda *_: None) == 1
    assert "--enrol-passkey needs --switches" in capsys.readouterr().out


def test_uptime_switched_off_checks_nothing_at_the_next_start(
    ui: Path, laptop_url: SecretStr, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    listed = tmp_path / "uptime.yaml"
    listed.write_text('eva:\n  - {url: "https://site.example/health"}\n', encoding="utf-8")
    with TestClient(start(ui, laptop_url), base_url="http://127.0.0.1") as client:
        uptime = {"kind": "connector", "id": "uptime"}
        assert client.post("/api/registry/switch-off", json=uptime, headers=OURS).status_code == 200
    capsys.readouterr()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"an address was checked although uptime is off: {request.url}")

    start(ui, laptop_url, ["--uptime", str(listed)], transport=httpx.MockTransport(refuse))
    assert "uptime is switched off on the Customize page, so it was not read." in capsys.readouterr().out


def test_supabase_switched_off_reads_nothing_at_the_next_start(
    ui: Path, laptop_url: SecretStr, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    listed = tmp_path / "supabase.yaml"
    listed.write_text("eva: {ref: abcdefghijklmnopqrst, token_env: SUPA_EVA}\n", encoding="utf-8")
    with TestClient(start(ui, laptop_url), base_url="http://127.0.0.1") as client:
        supabase = {"kind": "connector", "id": "supabase"}
        assert client.post("/api/registry/switch-off", json=supabase, headers=OURS).status_code == 200
    capsys.readouterr()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Supabase was read although switched off: {request.url}")

    start(
        ui,
        laptop_url,
        ["--supabase", str(listed)],
        environ={"SUPA_EVA": "sbp_" + "f" * 40},
        transport=httpx.MockTransport(refuse),
    )
    assert "supabase is switched off on the Customize page, so it was not read." in capsys.readouterr().out


NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


# ---------------------------------------------------------------- Board inspection (ADR-039, amended 24 Oct)


LAPTOP = WorkspaceDirectory(laptop_db.LAPTOP_WORKSPACES)


def _switches(url: SecretStr) -> dashboard_switches.LaptopSwitches:
    return dashboard_switches.LaptopSwitches(
        url,
        dashboard.load_plugins([]),
        {},
        lambda: NOW,
        site=Site(origin="http://localhost:8765"),
    )


def test_a_board_inspection_is_written_to_the_audit_log(laptop_url: SecretStr) -> None:
    eva = next(w for w in laptop_db.LAPTOP_WORKSPACES if w.slug == "eva")

    async def body() -> list[AuditRow]:
        switches = _switches(laptop_url)
        try:
            await switches.recorder(LAPTOP)("eva", "failed_build_log", "eva")
        finally:
            await switches.close()
        db = await Database.open(DatabaseSettings(dsn=laptop_url))
        try:
            return await AuditRows(db.scoped(laptop_db.LAPTOP_TENANT)).all_rows()
        finally:
            await db.close()

    looks = [e for e in run(body()) if e.action == "board.inspection"]
    assert len(looks) == 1
    assert (looks[0].project_id, looks[0].target_ref) == (eva.workspace_id, "failed_build_log:eva")
    assert looks[0].details == {"workspace": "eva", "tool": "failed_build_log"}


def test_a_look_at_an_unknown_workspace_raises_and_writes_nothing(laptop_url: SecretStr) -> None:
    async def body() -> None:
        switches = _switches(laptop_url)
        try:
            await switches.recorder(LAPTOP)("nope", "failed_build_log", "nope")
        finally:
            await switches.close()

    with pytest.raises(KeyError):
        run(body())


def test_the_agents_tab_and_removal_reach_the_laptop_database(laptop_url: SecretStr) -> None:
    eva = next(w for w in laptop_db.LAPTOP_WORKSPACES if w.slug == "eva")
    on = SwitchIn(enabled=True, cap_minor=500)

    async def body() -> list[object]:
        switches = _switches(laptop_url)
        try:
            agents = [
                switches.roles(),
                (await switches.view(eva)).agents,
                await switches.give(eva, "monitoring"),  # the library here is empty
                await switches.set_agent(eva, uuid4(), on, None),
                await switches.set_workspace(eva, on, None),
            ]
            with pytest.raises(PasskeyError) as caught:  # no passkey set up yet
                await switches.options("step:workspace:x:archive")
            removal = [
                len(await switches.active()),
                await switches.archived(),
                (await switches.archive(eva, None)).verdict,
                await switches.restore(eva, None),
                await switches.erase(eva, None),
            ]
        finally:
            await switches.close()
        return [*agents, caught.value.code, *removal]

    assert run(body()) == [
        (),
        (),
        AgentVerdict.UNKNOWN_ROLE,
        AgentVerdict.UNKNOWN_AGENT,
        AgentVerdict.NEEDS_PASSKEY,
        "registry.no_passkey",
        len(laptop_db.LAPTOP_WORKSPACES),
        [],
        LifecycleVerdict.NEEDS_PASSKEY,
        LifecycleVerdict.NEEDS_PASSKEY,
        LifecycleVerdict.NEEDS_PASSKEY,
    ]


def test_the_daily_summaries_reach_the_laptop_database(
    ui: Path, laptop_url: SecretStr, capsys: pytest.CaptureFixture[str]
) -> None:
    early = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)  # 04:00 in Dubai: nothing is due yet
    with TestClient(start(ui, laptop_url, clock=lambda: early), base_url="http://127.0.0.1") as client:
        assert client.get("/api/summaries").json() == {"connected": True, "workspaces": {}}
        made = client.post("/api/summaries/make", json={}, headers=OURS)
        assert made.json() == {"made": len(laptop_db.LAPTOP_WORKSPACES), "failed": 0}
        lines = client.get("/api/summaries").json()["workspaces"]
        assert set(lines) == {w.slug for w in laptop_db.LAPTOP_WORKSPACES}
        (day,) = client.get("/api/workspaces/eva/summaries").json()["days"]
        assert day["summary"]["sample_left_out"]  # sample cards are named, never counted
        assert client.get("/api/briefings").json() == {"connected": True, "briefings": []}
    said = capsys.readouterr().out
    assert "no ntfy topic, so the morning briefing is kept on the dashboard only" in said
    assert "daily summaries at 05:00 and the briefing at 07:00 (Asia/Dubai)" in said


def test_an_unknown_briefing_zone_is_refused(ui: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert dashboard.main(["--briefing-zone", "Mars/Olympus"], static_dir=ui, serve=lambda *_: None) == 1
    assert "'Mars/Olympus' is not a time zone Agent Zaaheen knows" in capsys.readouterr().out

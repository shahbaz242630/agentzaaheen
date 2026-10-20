"""The dashboard with ``--switches`` against a real database (ADR-030, F06 slice 2b).

The laptop database's set-up runs on the test server (in its own throwaway
database); then the dashboard is started for real, an item is switched off over
HTTP, and the switch is still off after a restart. A connector switched off is
not read at the next start.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tests.fixtures.soft_authenticator import SoftAuthenticator
from tests.integration.conftest import Server, run
from tools import dashboard, laptop_db
from yaqzan.core.api.app import REQUEST_HEADER
from yaqzan.core.store import StoreError, StoreErrorCode

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
            await conn.execute("DELETE FROM yaqzan.registry_switches")
        finally:
            await conn.close()

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        run(delete_switches())


def test_a_switch_turned_off_stays_off_after_a_restart(ui: Path, laptop_url: SecretStr) -> None:
    with TestClient(start(ui, laptop_url), base_url="http://127.0.0.1") as client:
        page = client.get("/api/registry").json()
        assert page["switches"] is True
        assert states(client) == {"github": "on", "paddle": "on", "triage-failing-build": "on"}
        response = client.post("/api/registry/switch-off", json=SKILL, headers=OURS)
        assert response.status_code == 200
        assert {s["id"]: s["state"] for s in response.json()["skills"]} == {"triage-failing-build": "off"}

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

"""Signing in to the dashboard over HTTP (ADR-SEC-010): every closed route asks for a session first."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from agentzaaheen.core.api import SamplePortfolio, create_app
from agentzaaheen.core.api.app import REQUEST_HEADER
from agentzaaheen.core.api.registry import registry_page
from agentzaaheen.core.api.session import ACTIVE_HEADER, COOKIE, OPEN_ROUTES, SessionGate
from agentzaaheen.core.auth import IDLE_AFTER, LONGEST, PasskeyError
from agentzaaheen.core.store import StoreError, StoreErrorCode
from tests.unit.test_api_switches import REGISTRY, FakeSwitches

NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
OURS = {REQUEST_HEADER: "1", "Origin": "http://localhost:8765"}
ACTIVE = {**OURS, ACTIVE_HEADER: "1"}
GOOD = {"id": "the-chairmans-passkey"}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class FakeSignIns:
    """Answers like the passkey check: ``GOOD`` signs in, anything else fails; records each audit."""

    def __init__(self) -> None:
        self.audits: list[str] = []
        self.no_passkey = False
        self.broken = False

    async def sign_in_options(self) -> dict[str, Any]:
        if self.no_passkey:
            raise PasskeyError("registry.no_passkey")
        return {"challenge": "c"}

    async def sign_in(self, credential: dict[str, Any]) -> bool:
        if self.broken:
            raise StoreError(StoreErrorCode.UNAVAILABLE, "down")
        return credential == GOOD

    async def signed_out(self, reason: str) -> None:
        if self.broken:
            raise StoreError(StoreErrorCode.UNAVAILABLE, "down")
        self.audits.append(reason)


def build(
    tmp_path: Path, sign_ins: FakeSignIns, clock: Clock, *, switches: FakeSwitches | None = None
) -> tuple[FastAPI, TestClient]:
    (tmp_path / "index.html").write_text("<!doctype html><title>Agent Zaaheen</title>", encoding="utf-8")
    app = create_app(
        SamplePortfolio(NOW),
        static_dir=tmp_path,
        registry=registry_page(REGISTRY, {}),
        switches=switches,
        session=SessionGate(sign_ins, clock),
    )
    return app, TestClient(app, base_url="http://localhost:8765")


def sign_in(client: TestClient, credential: dict[str, Any] = GOOD) -> httpx.Response:
    client.post("/api/session/options", json={}, headers=OURS)
    answer: httpx.Response = client.post("/api/session", json={"credential": credential}, headers=OURS)
    return answer


def filled(path: str) -> str:
    """A route template with each blank filled (a known workspace, a well-formed id)."""
    path = path.replace("{slug}", "zaaheen")
    return re.sub(r"\{[a-z_]+\}", "00000000-0000-0000-0000-000000000000", path)


def test_every_api_route_but_the_open_ones_refuses_without_a_session(tmp_path: Path) -> None:
    app, client = build(tmp_path, FakeSignIns(), Clock())
    checked = 0
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        for method in sorted(route.methods or ()):
            if (method, route.path) in OPEN_ROUTES:
                continue
            answer = client.request(method, filled(route.path), json={}, headers=ACTIVE)
            assert (answer.status_code, answer.json()) == (401, {"detail": "session.needed"}), route.path
            checked += 1
    assert checked > 40  # every part of the dashboard is behind it, not a handful


def test_the_page_itself_loads_without_a_session_so_the_sign_in_screen_can_show(tmp_path: Path) -> None:
    _, client = build(tmp_path, FakeSignIns(), Clock())
    assert client.get("/").status_code == 200
    assert client.get("/api/session").json() == {"required": True, "signed_in": False}


def test_signing_in_opens_every_route_with_a_strict_cookie(tmp_path: Path) -> None:
    _, client = build(tmp_path, FakeSignIns(), Clock())
    answer = sign_in(client)
    assert answer.json() == {"required": True, "signed_in": True}
    cookie = answer.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert f"Max-Age={int(LONGEST.total_seconds())}" in cookie
    assert "Secure" not in cookie  # http://localhost: a Secure cookie would not come back
    assert client.get("/api/registry").status_code == 200
    assert client.get("/api/session").json() == {"required": True, "signed_in": True}


def test_over_https_the_cookie_is_secure(tmp_path: Path) -> None:
    app, _ = build(tmp_path, FakeSignIns(), Clock())
    client = TestClient(app, base_url="https://localhost")
    client.post("/api/session/options", json={}, headers={**OURS, "Origin": "https://localhost"})
    answer = client.post(
        "/api/session", json={"credential": GOOD}, headers={**OURS, "Origin": "https://localhost"}
    )
    assert "Secure" in answer.headers["set-cookie"]


def test_a_failed_sign_in_gives_no_session(tmp_path: Path) -> None:
    _, client = build(tmp_path, FakeSignIns(), Clock())
    answer = sign_in(client, {"id": "someone-else"})
    assert (answer.status_code, answer.json()) == (403, {"detail": "session.sign_in_failed"})
    assert "set-cookie" not in answer.headers
    assert client.get("/api/registry").status_code == 401


def test_a_made_up_cookie_opens_nothing(tmp_path: Path) -> None:
    _, client = build(tmp_path, FakeSignIns(), Clock())
    client.cookies.set(COOKIE, "made-up")
    assert client.get("/api/registry").status_code == 401


def test_five_failures_make_sign_in_wait_a_minute_even_for_the_right_passkey(tmp_path: Path) -> None:
    clock = Clock()
    _, client = build(tmp_path, FakeSignIns(), clock)
    for _ in range(5):
        assert sign_in(client, {"id": "guess"}).status_code == 403
    waiting = sign_in(client)
    assert (waiting.status_code, waiting.json()) == (429, {"detail": "session.wait"})
    assert client.post("/api/session/options", json={}, headers=OURS).status_code == 429
    clock.now += timedelta(minutes=1)
    assert sign_in(client).status_code == 200


def test_signing_out_closes_the_routes_and_is_audited(tmp_path: Path) -> None:
    sign_ins = FakeSignIns()
    _, client = build(tmp_path, sign_ins, Clock())
    sign_in(client)
    out = client.post("/api/session/sign-out", json={}, headers=OURS)
    assert out.json() == {"required": True, "signed_in": False}
    assert client.get("/api/registry").status_code == 401
    assert sign_ins.audits == ["session.by_button"]
    client.post("/api/session/sign-out", json={}, headers=OURS)  # signed out already: nothing more
    assert sign_ins.audits == ["session.by_button"]


def test_3_idle_hours_sign_out_unless_a_person_acted(tmp_path: Path) -> None:
    clock = Clock()
    sign_ins = FakeSignIns()
    _, client = build(tmp_path, sign_ins, clock)
    sign_in(client)
    clock.now += timedelta(hours=2)
    assert client.get("/api/registry").status_code == 200  # no mark: does not count as an action
    clock.now += timedelta(hours=1)
    assert client.get("/api/registry", headers={ACTIVE_HEADER: "1"}).status_code == 401
    assert sign_ins.audits == ["session.idle"]

    sign_in(client)
    clock.now += IDLE_AFTER - timedelta(minutes=1)
    assert client.get("/api/registry", headers={ACTIVE_HEADER: "1"}).status_code == 200
    clock.now += timedelta(hours=2)
    assert client.get("/api/registry").status_code == 200


def test_12_hours_sign_out_however_busy(tmp_path: Path) -> None:
    clock = Clock()
    sign_ins = FakeSignIns()
    _, client = build(tmp_path, sign_ins, clock)
    sign_in(client)
    for _ in range(11):
        clock.now += timedelta(hours=1)
        assert client.get("/api/registry", headers={ACTIVE_HEADER: "1"}).status_code == 200
    clock.now += timedelta(hours=1)
    assert client.get("/api/registry", headers={ACTIVE_HEADER: "1"}).status_code == 401
    assert sign_ins.audits == ["session.limit"]


def test_a_fourth_browser_ends_the_oldest_sign_in(tmp_path: Path) -> None:
    sign_ins = FakeSignIns()
    app, first = build(tmp_path, sign_ins, Clock())
    others = [TestClient(app, base_url="http://localhost:8765") for _ in range(3)]
    sign_in(first)
    for other in others:
        sign_in(other)
    assert first.get("/api/registry").status_code == 401
    assert all(other.get("/api/registry").status_code == 200 for other in others)
    assert sign_ins.audits == ["session.replaced"]


def test_signing_in_again_in_the_same_browser_keeps_one_session(tmp_path: Path) -> None:
    sign_ins = FakeSignIns()
    app, client = build(tmp_path, sign_ins, Clock())
    others = [TestClient(app, base_url="http://localhost:8765") for _ in range(2)]
    for other in others:
        sign_in(other)
    for _ in range(3):
        sign_in(client)
    assert all(c.get("/api/registry").status_code == 200 for c in [client, *others])
    assert sign_ins.audits == []


def test_sign_in_is_only_for_our_own_page(tmp_path: Path) -> None:
    _, client = build(tmp_path, FakeSignIns(), Clock())
    foreign = {REQUEST_HEADER: "1", "Origin": "http://evil.example"}
    assert client.post("/api/session/options", json={}, headers=foreign).status_code == 403
    assert client.post("/api/session", json={"credential": GOOD}, headers=foreign).status_code == 403
    assert client.post("/api/session", json={"credential": GOOD}).status_code == 403  # no header


def test_without_a_passkey_sign_in_says_so(tmp_path: Path) -> None:
    sign_ins = FakeSignIns()
    sign_ins.no_passkey = True
    _, client = build(tmp_path, sign_ins, Clock())
    answer = client.post("/api/session/options", json={}, headers=OURS)
    assert (answer.status_code, answer.json()) == (409, {"detail": "registry.no_passkey"})


def test_a_new_passkey_made_with_the_start_up_code_signs_in(tmp_path: Path) -> None:
    _, client = build(tmp_path, FakeSignIns(), Clock(), switches=FakeSwitches())
    assert client.get("/api/passkey").status_code == 200
    assert (
        client.post("/api/passkey/enrol/options", json={"code": "0a1b2c3d"}, headers=OURS).status_code == 200
    )
    done = client.post(
        "/api/passkey/enrol", json={"code": "0a1b2c3d", "credential": {"id": "n"}}, headers=OURS
    )
    assert done.status_code == 200
    assert client.get("/api/registry").status_code == 200


def test_a_wrong_start_up_code_signs_nobody_in(tmp_path: Path) -> None:
    switches = FakeSwitches()
    switches.passkey_error = "passkey.wrong_code"
    _, client = build(tmp_path, FakeSignIns(), Clock(), switches=switches)
    done = client.post(
        "/api/passkey/enrol", json={"code": "ffffffff", "credential": {"id": "n"}}, headers=OURS
    )
    assert done.status_code == 403
    assert "set-cookie" not in done.headers
    assert client.get("/api/registry").status_code == 401


def test_a_database_failure_is_explained_and_never_signs_in(tmp_path: Path) -> None:
    sign_ins = FakeSignIns()
    sign_ins.broken = True
    _, client = build(tmp_path, sign_ins, Clock())
    answer = sign_in(client)
    assert (answer.status_code, answer.json()) == (503, {"detail": "session.store_unavailable"})
    assert client.get("/api/registry").status_code == 401


def test_a_sign_out_still_happens_when_its_audit_row_fails(tmp_path: Path) -> None:
    sign_ins = FakeSignIns()
    _, client = build(tmp_path, sign_ins, Clock())
    sign_in(client)
    sign_ins.broken = True
    assert client.post("/api/session/sign-out", json={}, headers=OURS).status_code == 200
    assert client.get("/api/registry").status_code == 401


@pytest.mark.parametrize("path", ["/api/registry", "/api/workspaces", "/api/session"])
def test_without_a_gate_nothing_is_asked(tmp_path: Path, path: str) -> None:
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    client = TestClient(create_app(SamplePortfolio(NOW), static_dir=tmp_path), base_url="http://127.0.0.1")
    assert client.get(path).status_code == 200
    assert client.get("/api/session").json() == {"required": False, "signed_in": False}
    refused = client.post("/api/session", json={"credential": GOOD}, headers={REQUEST_HEADER: "1"})
    assert (refused.status_code, refused.json()) == (404, {"detail": "session.not_needed"})

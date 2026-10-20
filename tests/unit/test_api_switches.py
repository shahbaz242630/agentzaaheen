"""The Customize page's switches over HTTP (ADR-030): only off, ask and withdraw; only from our own page."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from yaqzan.core.api import SamplePortfolio, create_app
from yaqzan.core.api.app import REQUEST_HEADER
from yaqzan.core.api.registry import PasskeyStatus, RegistryPage, registry_page
from yaqzan.core.auth import PasskeyError
from yaqzan.core.policy import Pending, Standing, Verdict
from yaqzan.core.registry import Kind, Layout, Plugin, Registry, Skill, State
from yaqzan.core.registry.models import Invocation
from yaqzan.core.store import StoreError, StoreErrorCode

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
OURS = {REQUEST_HEADER: "1", "Origin": "http://127.0.0.1"}
HASH = "a" * 64
REGISTRY = Registry(
    skills=(
        Skill(id="a-skill", description="x", invocation=Invocation.AUTO, requires=(), fingerprint=HASH),
    ),
    plugins=(
        Plugin(
            id="outside", description="x", layout=Layout.CLAUDE, skills=0, connectors=0, commands=0,
            agents=0, hooks=False, fingerprint=HASH,
        ),
    ),
)  # fmt: skip


class FakeSwitches:
    """Records each call and answers with a chosen verdict (or a database failure)."""

    def __init__(self, verdict: Verdict = Verdict.DONE, *, broken: bool = False) -> None:
        self.verdict = verdict
        self.broken = broken
        self.calls: list[tuple[str, object]] = []
        self.request_id = uuid4()
        self.enrolled = False
        self.passkey_error: str | None = None

    async def page(self) -> RegistryPage:
        if self.broken:
            raise StoreError(StoreErrorCode.UNAVAILABLE, "down")
        standing = Standing(
            states={(Kind.SKILL, "a-skill"): State.OFF, (Kind.PLUGIN, "outside"): State.VIEW_ONLY},
            pending=(
                Pending(
                    id=self.request_id, kind=Kind.SKILL, item_id="a-skill", requested_at=NOW, stale=False
                ),
            ),
        )
        return registry_page(REGISTRY, {}, standing)

    async def switch_off(self, kind: Kind, item_id: str) -> Verdict:
        self.calls.append(("off", (kind, item_id)))
        return self.verdict

    async def request_on(self, kind: Kind, item_id: str) -> Verdict:
        self.calls.append(("ask", (kind, item_id)))
        return self.verdict

    async def withdraw(self, request_id: UUID) -> Verdict:
        self.calls.append(("withdraw", request_id))
        return self.verdict

    async def refuse(self, request_id: UUID) -> Verdict:
        self.calls.append(("refuse", request_id))
        return self.verdict

    async def approve(self, request_id: UUID, credential: dict[str, Any]) -> Verdict:
        self.calls.append(("approve", (request_id, credential)))
        return self.verdict

    async def approve_options(self, request_id: UUID) -> dict[str, Any]:
        self.calls.append(("approve_options", request_id))
        if self.passkey_error:
            raise PasskeyError(self.passkey_error)
        return {"challenge": "abc"}

    async def passkey(self) -> PasskeyStatus:
        return PasskeyStatus(enrolled=self.enrolled, enrolment_open=not self.enrolled)

    async def enrol_options(self, code: str) -> dict[str, Any]:
        self.calls.append(("enrol_options", code))
        if self.passkey_error:
            raise PasskeyError(self.passkey_error)
        return {"challenge": "xyz"}

    async def enrol(self, code: str, credential: dict[str, Any]) -> None:
        self.calls.append(("enrol", (code, credential)))
        if self.passkey_error:
            raise PasskeyError(self.passkey_error)
        self.enrolled = True


def client(switches: FakeSwitches | None) -> TestClient:
    page = registry_page(REGISTRY, {})
    app = create_app(SamplePortfolio(NOW), registry=page, switches=switches)
    return TestClient(app, base_url="http://127.0.0.1")


ROUTES = [
    ("/api/registry/switch-off", {"kind": "skill", "id": "a-skill"}, ("off", (Kind.SKILL, "a-skill"))),
    ("/api/registry/requests", {"kind": "skill", "id": "a-skill"}, ("ask", (Kind.SKILL, "a-skill"))),
]


def test_without_a_database_the_page_shows_defaults_and_cannot_switch() -> None:
    c = client(None)
    page = RegistryPage.model_validate(c.get("/api/registry").json())
    assert (page.switches, page.requests) == (False, ())
    assert [s.state for s in page.skills] == [State.ON]
    assert [p.state for p in page.plugins] == [State.VIEW_ONLY]
    for path, body, _ in ROUTES:
        response = c.post(path, json=body, headers=OURS)
        assert (response.status_code, response.json()["detail"]) == (503, "registry.read_only")
    response = c.post(f"/api/registry/requests/{uuid4()}/withdraw", json={}, headers=OURS)
    assert response.status_code == 503


def test_with_a_database_the_page_is_read_fresh() -> None:
    switches = FakeSwitches()
    page = RegistryPage.model_validate(client(switches).get("/api/registry").json())
    assert page.switches
    assert [s.state for s in page.skills] == [State.OFF]
    assert [(r.id, r.item_id) for r in page.requests] == [(switches.request_id, "a-skill")]


@pytest.mark.parametrize(("path", "body", "call"), ROUTES)
def test_a_switch_that_works_returns_the_new_page(path: str, body: object, call: tuple[str, object]) -> None:
    switches = FakeSwitches()
    response = client(switches).post(path, json=body, headers=OURS)
    assert response.status_code == 200
    assert RegistryPage.model_validate(response.json()).switches
    assert switches.calls == [call]


def test_withdraw_names_the_request() -> None:
    switches = FakeSwitches()
    request_id = uuid4()
    response = client(switches).post(f"/api/registry/requests/{request_id}/withdraw", json={}, headers=OURS)
    assert response.status_code == 200
    assert switches.calls == [("withdraw", request_id)]


@pytest.mark.parametrize("verdict", [v for v in Verdict if v is not Verdict.DONE])
def test_a_refusal_says_why(verdict: Verdict) -> None:
    response = client(FakeSwitches(verdict)).post(ROUTES[0][0], json=ROUTES[0][1], headers=OURS)
    assert (response.status_code, response.json()["detail"]) == (409, verdict.value)


def test_a_database_failure_is_unavailable_and_says_nothing_more() -> None:
    c = client(FakeSwitches(broken=True))
    for response in (c.get("/api/registry"), c.post(ROUTES[0][0], json=ROUTES[0][1], headers=OURS)):
        assert (response.status_code, response.json()) == (503, {"detail": "registry.store_unavailable"})


@pytest.mark.parametrize(
    "headers",
    [
        {},  # no custom header: a plain form post from another site
        {REQUEST_HEADER: "1", "Origin": "http://evil.example"},
    ],
)
def test_another_web_page_cannot_switch_anything(headers: dict[str, str]) -> None:
    switches = FakeSwitches()
    c = client(switches)
    for path, body, _ in ROUTES:
        assert c.post(path, json=body, headers=headers).status_code == 403
    assert c.post(f"/api/registry/requests/{uuid4()}/withdraw", json={}, headers=headers).status_code == 403
    assert switches.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "agent", "id": "a-skill"},  # not a kind
        {"kind": "skill", "id": "../etc"},  # not an id
        {"kind": "skill", "id": "a-skill", "enabled": True},  # nothing else may be sent
        {"kind": "skill"},
    ],
)
def test_odd_bodies_are_refused_before_any_switch(body: object) -> None:
    switches = FakeSwitches()
    assert client(switches).post(ROUTES[0][0], json=body, headers=OURS).status_code == 422
    assert switches.calls == []


def test_a_request_id_must_be_a_uuid() -> None:
    switches = FakeSwitches()
    response = client(switches).post("/api/registry/requests/not-a-uuid/withdraw", json={}, headers=OURS)
    assert response.status_code == 422
    assert switches.calls == []


def test_the_only_way_on_is_approving_a_request_with_a_signed_answer() -> None:
    switches = FakeSwitches()
    c = client(switches)
    for path in ("/api/registry/switch-on", "/api/registry/approve"):
        assert c.post(path, json={"kind": "skill", "id": "a-skill"}, headers=OURS).status_code in {404, 405}
    approve = f"/api/registry/requests/{uuid4()}/approve"
    assert c.post(approve, json={}, headers=OURS).status_code == 422  # no signed answer
    assert c.post(approve, json={"credential": {"id": "x"}}, headers={}).status_code == 403  # another site
    assert switches.calls == []


def test_approve_hands_the_signed_answer_over_and_returns_the_new_page() -> None:
    switches = FakeSwitches()
    request_id = uuid4()
    credential = {"id": "abc", "response": {"signature": "s"}}
    response = client(switches).post(
        f"/api/registry/requests/{request_id}/approve", json={"credential": credential}, headers=OURS
    )
    assert response.status_code == 200
    assert switches.calls == [("approve", (request_id, credential))]


def test_a_failed_passkey_check_is_refused_with_its_reason() -> None:
    response = client(FakeSwitches(Verdict.PASSKEY_FAILED)).post(
        f"/api/registry/requests/{uuid4()}/approve", json={"credential": {"id": "x"}}, headers=OURS
    )
    assert (response.status_code, response.json()["detail"]) == (409, "registry.passkey_failed")


def test_refuse_needs_no_passkey() -> None:
    switches = FakeSwitches()
    request_id = uuid4()
    response = client(switches).post(f"/api/registry/requests/{request_id}/refuse", json={}, headers=OURS)
    assert response.status_code == 200
    assert switches.calls == [("refuse", request_id)]


def test_approve_options_are_passed_through_or_refused_with_a_reason() -> None:
    switches = FakeSwitches()
    request_id = uuid4()
    path = f"/api/registry/requests/{request_id}/approve/options"
    assert client(switches).post(path, json={}, headers=OURS).json() == {"options": {"challenge": "abc"}}
    switches.passkey_error = "registry.stale"
    response = client(switches).post(path, json={}, headers=OURS)
    assert (response.status_code, response.json()["detail"]) == (409, "registry.stale")


def test_passkey_status_and_set_up() -> None:
    switches = FakeSwitches()
    c = client(switches)
    assert c.get("/api/passkey").json() == {"enrolled": False, "enrolment_open": True}
    options = c.post("/api/passkey/enrol/options", json={"code": "0a1b2c3d"}, headers=OURS)
    assert options.json() == {"options": {"challenge": "xyz"}}
    done = c.post("/api/passkey/enrol", json={"code": "0a1b2c3d", "credential": {"id": "n"}}, headers=OURS)
    assert done.json() == {"enrolled": True, "enrolment_open": False}
    assert switches.calls == [("enrol_options", "0a1b2c3d"), ("enrol", ("0a1b2c3d", {"id": "n"}))]


@pytest.mark.parametrize("code", ["passkey.wrong_code", "passkey.enrol_closed"])
def test_a_wrong_or_closed_set_up_is_forbidden(code: str) -> None:
    switches = FakeSwitches()
    switches.passkey_error = code
    response = client(switches).post("/api/passkey/enrol/options", json={"code": "0a1b2c3d"}, headers=OURS)
    assert (response.status_code, response.json()["detail"]) == (403, code)


@pytest.mark.parametrize("body", [{"code": "short"}, {"code": "0A1B2C3D"}, {"code": "0a1b2c3d", "x": 1}, {}])
def test_an_odd_set_up_code_is_refused_before_anything(body: object) -> None:
    switches = FakeSwitches()
    assert client(switches).post("/api/passkey/enrol/options", json=body, headers=OURS).status_code == 422
    assert switches.calls == []


def test_without_a_database_there_is_no_passkey() -> None:
    c = client(None)
    assert c.get("/api/passkey").json() == {"enrolled": False, "enrolment_open": False}
    response = c.post("/api/passkey/enrol/options", json={"code": "0a1b2c3d"}, headers=OURS)
    assert (response.status_code, response.json()["detail"]) == (503, "registry.read_only")

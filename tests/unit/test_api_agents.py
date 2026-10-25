"""The Agents tab's routes: behind the workspace door; a riskier change needs a signature for exactly it."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi.testclient import TestClient

from agentzaaheen.core.api import SamplePortfolio, create_app
from agentzaaheen.core.api.agents import RoleChoice, SwitchIn, WorkspaceAgentsOut, purpose
from agentzaaheen.core.api.app import REQUEST_HEADER
from agentzaaheen.core.auth import PasskeyError
from agentzaaheen.core.policy import AgentVerdict
from agentzaaheen.core.store import StoreError, StoreErrorCode, WorkspaceRow

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
OURS = {REQUEST_HEADER: "1", "Origin": "http://127.0.0.1"}
AGENT = UUID("11111111-2222-4333-8444-555555555555")


class FakeAgents:
    """Records what it was asked; a riskier change without a signature needs a passkey, like the real one."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def roles(self) -> tuple[RoleChoice, ...]:
        return (RoleChoice(id="monitoring", title="Monitoring", mission="Watch.", permission="read"),)

    async def view(self, workspace: WorkspaceRow) -> WorkspaceAgentsOut:
        self.calls.append(("view", workspace.slug))
        return WorkspaceAgentsOut(enabled=False, cap_minor=None, spent_minor=0, agents=(), switches=True)

    async def give(self, workspace: WorkspaceRow, role_id: str) -> AgentVerdict:
        self.calls.append(("give", (workspace.slug, role_id)))
        return AgentVerdict.DONE if role_id == "monitoring" else AgentVerdict.UNKNOWN_ROLE

    async def set_agent(
        self, workspace: WorkspaceRow, agent_id: UUID, change: SwitchIn, credential: dict[str, Any] | None
    ) -> AgentVerdict:
        self.calls.append(("set_agent", (workspace.slug, agent_id, change, credential)))
        return AgentVerdict.NEEDS_PASSKEY if change.enabled and credential is None else AgentVerdict.DONE

    async def set_workspace(
        self, workspace: WorkspaceRow, change: SwitchIn, credential: dict[str, Any] | None
    ) -> AgentVerdict:
        self.calls.append(("set_workspace", (workspace.slug, change, credential)))
        return AgentVerdict.DONE

    async def options(self, step: str) -> dict[str, Any]:
        self.calls.append(("options", step))
        return {"challenge": "c"}


def client(agents: FakeAgents | None) -> TestClient:
    return TestClient(create_app(SamplePortfolio(NOW), agents=agents), base_url="http://127.0.0.1")


def post(c: TestClient, path: str, body: object) -> httpx.Response:
    response: httpx.Response = c.post(path, json=body, headers=OURS)
    return response


def test_the_library_and_a_workspaces_agents_are_listed() -> None:
    fake = FakeAgents()
    c = client(fake)
    assert c.get("/api/roles").json() == [
        {"id": "monitoring", "title": "Monitoring", "mission": "Watch.", "permission": "read"}
    ]
    assert c.get("/api/workspaces/eva/agents").json()["switches"] is True
    assert fake.calls == [("view", "eva")]


def test_without_a_database_the_tab_is_empty_and_read_only() -> None:
    c = client(None)
    assert c.get("/api/roles").json() == []
    assert c.get("/api/workspaces/eva/agents").json() == {
        "enabled": False, "cap_minor": None, "spent_minor": 0, "agents": [], "switches": False,
    }  # fmt: skip
    response = post(c, "/api/workspaces/eva/agents", {"role_id": "monitoring"})
    assert (response.status_code, response.json()) == (503, {"detail": "agents.read_only"})


def test_giving_an_agent_goes_to_the_workspace_in_the_address() -> None:
    fake = FakeAgents()
    assert post(client(fake), "/api/workspaces/eva/agents", {"role_id": "monitoring"}).status_code == 200
    assert ("give", ("eva", "monitoring")) in fake.calls


def test_an_unknown_workspace_is_refused_before_anything_is_asked() -> None:
    fake = FakeAgents()
    assert post(client(fake), "/api/workspaces/nope/agents", {"role_id": "monitoring"}).status_code == 404
    assert fake.calls == []


def test_a_riskier_change_without_a_signature_answers_needs_passkey() -> None:
    fake = FakeAgents()
    response = post(client(fake), f"/api/workspaces/eva/agents/{AGENT}", {"enabled": True, "cap_minor": 500})
    assert (response.status_code, response.json()) == (409, {"detail": "agents.needs_passkey"})


def test_the_challenge_names_exactly_this_change() -> None:
    fake = FakeAgents()
    c = client(fake)
    post(c, f"/api/workspaces/eva/agents/{AGENT}/options", {"enabled": True, "cap_minor": 500})
    post(c, "/api/workspaces/eva/settings/options", {"enabled": True, "cap_minor": None})
    steps = [arg for name, arg in fake.calls if name == "options"]
    assert steps[0] == f"step:agent:{AGENT}:on:500"
    assert steps[1].startswith("step:workspace:")
    assert steps[1].endswith(":on:none")


def test_a_signature_for_one_cap_names_a_different_purpose_from_another() -> None:
    one, other = uuid4(), uuid4()
    assert purpose("agent", one, SwitchIn(enabled=True, cap_minor=500)) != purpose(
        "agent", one, SwitchIn(enabled=True, cap_minor=50000)
    )
    assert purpose("agent", one, SwitchIn(enabled=True)) != purpose("agent", other, SwitchIn(enabled=True))


def test_a_signed_change_is_passed_on_with_its_signature() -> None:
    fake = FakeAgents()
    body = {"enabled": True, "cap_minor": 500, "credential": {"id": "x"}}
    assert post(client(fake), f"/api/workspaces/eva/agents/{AGENT}", body).status_code == 200
    (_, (slug, agent_id, change, credential)) = next(c for c in fake.calls if c[0] == "set_agent")
    assert (slug, agent_id, change, credential) == (
        "eva",
        AGENT,
        SwitchIn(enabled=True, cap_minor=500),
        {"id": "x"},
    )


def test_caps_must_be_sensible_numbers() -> None:
    fake = FakeAgents()
    c = client(fake)
    for cap in (-1, 10**12, "lots"):
        assert (
            post(c, "/api/workspaces/eva/settings", {"enabled": False, "cap_minor": cap}).status_code == 422
        )
    assert not [call for call in fake.calls if call[0] == "set_workspace"]


def test_changes_need_our_own_page() -> None:
    fake = FakeAgents()
    response = client(fake).post("/api/workspaces/eva/agents", json={"role_id": "monitoring"})
    assert response.status_code == 403
    assert fake.calls == []


class Broken(FakeAgents):
    """A database that does not answer, or a passkey problem, on every change."""

    def __init__(self, failure: Exception) -> None:
        super().__init__()
        self.failure = failure

    async def give(self, workspace: WorkspaceRow, role_id: str) -> AgentVerdict:
        raise self.failure


def test_a_database_that_does_not_answer_or_a_passkey_problem_is_said_plainly() -> None:
    down = post(
        client(Broken(StoreError(StoreErrorCode.UNAVAILABLE, "down"))),
        "/api/workspaces/eva/agents",
        {"role_id": "monitoring"},
    )
    assert (down.status_code, down.json()) == (503, {"detail": "agents.store_unavailable"})
    no_key = post(
        client(Broken(PasskeyError("registry.no_passkey"))),
        "/api/workspaces/eva/agents",
        {"role_id": "monitoring"},
    )
    assert (no_key.status_code, no_key.json()) == (409, {"detail": "registry.no_passkey"})


def test_without_a_database_nothing_can_be_changed() -> None:
    refused = post(client(None), "/api/workspaces/eva/agents", {"role_id": "monitoring"})
    assert (refused.status_code, refused.json()) == (503, {"detail": "agents.read_only"})

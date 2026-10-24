"""Removing a project: archive behind the door, bring back or delete from the archived list, each signed."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from yaqzan.core.api import SamplePortfolio, create_app
from yaqzan.core.api.app import REQUEST_HEADER
from yaqzan.core.api.removal import purpose
from yaqzan.core.auth import PasskeyError
from yaqzan.core.policy import LifecycleVerdict, Removal
from yaqzan.core.store import Archived, StoreError, StoreErrorCode, WorkspaceRow

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
OURS = {REQUEST_HEADER: "1", "Origin": "http://127.0.0.1"}
SIGNED = {"id": "cred", "response": {"signature": "sig"}}


class FakeRemoval:
    """Moves workspaces between lists like the real one; nothing happens without a signature."""

    def __init__(self) -> None:
        self.live: list[WorkspaceRow] = []
        self.gone: list[WorkspaceRow] = []
        self.erased: list[str] = []
        self.calls: list[tuple[str, Any]] = []
        self.broken = False

    async def active(self) -> list[WorkspaceRow]:
        return list(self.live)

    async def archived(self) -> list[WorkspaceRow]:
        if self.broken:
            raise StoreError(StoreErrorCode.UNAVAILABLE, "down")
        return list(self.gone)

    async def archive(self, workspace: WorkspaceRow, credential: dict[str, Any] | None) -> Removal:
        self.calls.append(("archive", (workspace.slug, credential)))
        if credential is None:
            return Removal(LifecycleVerdict.NEEDS_PASSKEY)
        self.live.remove(workspace)
        self.gone.append(workspace)
        return Removal(LifecycleVerdict.DONE, Archived(agents_stopped=2, keys_deleted=3))

    async def restore(self, workspace: WorkspaceRow, credential: dict[str, Any] | None) -> LifecycleVerdict:
        self.calls.append(("restore", (workspace.slug, credential)))
        if credential is None:
            return LifecycleVerdict.NEEDS_PASSKEY
        self.gone.remove(workspace)
        self.live.append(workspace)
        return LifecycleVerdict.DONE

    async def erase(self, workspace: WorkspaceRow, credential: dict[str, Any] | None) -> LifecycleVerdict:
        self.calls.append(("erase", (workspace.slug, credential)))
        if credential is None:
            return LifecycleVerdict.NEEDS_PASSKEY
        self.gone.remove(workspace)
        self.erased.append(workspace.slug)
        return LifecycleVerdict.DONE

    async def options(self, step: str) -> dict[str, Any]:
        self.calls.append(("options", step))
        return {"challenge": "c"}


def client(removal: FakeRemoval | None) -> TestClient:
    app = create_app(SamplePortfolio(NOW), removal=removal)
    if removal is not None:
        removal.live = list(app.state.workspaces.all())
    return TestClient(app, base_url="http://127.0.0.1")


def post(c: TestClient, path: str, body: object = None) -> httpx.Response:
    response: httpx.Response = c.post(path, json=body or {}, headers=OURS)
    return response


def eva(fake: FakeRemoval) -> WorkspaceRow:
    return next(w for w in [*fake.live, *fake.gone] if w.slug == "eva")


def test_archiving_asks_windows_hello_then_the_address_stops_answering() -> None:
    fake = FakeRemoval()
    c = client(fake)
    asked = post(c, "/api/workspaces/eva/archive")
    assert (asked.status_code, asked.json()) == (409, {"detail": "workspaces.needs_passkey"})
    assert c.get("/api/workspaces/eva").status_code == 200  # nothing changed yet

    options = post(c, "/api/workspaces/eva/archive/options")
    assert options.json() == {"options": {"challenge": "c"}}
    done = post(c, "/api/workspaces/eva/archive", {"credential": SIGNED})
    assert done.json() == {"agents_stopped": 2, "keys_deleted": 3}

    assert c.get("/api/workspaces/eva").json() == {"detail": "workspace.not_found"}
    assert "eva" not in [w["id"] for w in c.get("/api/workspaces").json()]
    assert c.get("/api/archived").json() == {"workspaces": [{"slug": "eva", "name": "Eva"}], "switches": True}
    assert ("options", purpose("archive", eva(fake))) in fake.calls


def test_bringing_back_puts_the_address_back() -> None:
    fake = FakeRemoval()
    c = client(fake)
    post(c, "/api/workspaces/eva/archive", {"credential": SIGNED})
    assert post(c, "/api/archived/eva/restore").json() == {"detail": "workspaces.needs_passkey"}
    assert post(c, "/api/archived/eva/restore", {"credential": SIGNED}).json() == {
        "workspaces": [],
        "switches": True,
    }
    assert c.get("/api/workspaces/eva").status_code == 200


def test_deleting_for_good_is_only_for_an_archived_workspace() -> None:
    fake = FakeRemoval()
    c = client(fake)
    # Still active: it has no address in the archive, so it cannot be deleted in one step.
    refused = post(c, "/api/archived/eva/erase", {"credential": SIGNED})
    assert (refused.status_code, refused.json()) == (404, {"detail": "workspace.not_archived"})
    assert not [call for call in fake.calls if call[0] == "erase"]

    row = eva(fake)
    post(c, "/api/workspaces/eva/archive", {"credential": SIGNED})
    assert post(c, "/api/archived/eva/erase").json() == {"detail": "workspaces.needs_passkey"}
    assert post(c, "/api/archived/eva/erase/options").json() == {"options": {"challenge": "c"}}
    assert post(c, "/api/archived/eva/erase", {"credential": SIGNED}).status_code == 200
    assert fake.erased == ["eva"]
    assert ("options", purpose("erase", row)) in fake.calls
    assert c.get("/api/archived").json()["workspaces"] == []


def test_each_signature_names_one_change_to_one_workspace() -> None:
    a = WorkspaceRow(uuid4(), "eva", "Eva")
    steps = {purpose(step, a) for step in ("archive", "restore", "erase")}
    assert len(steps) == 3
    assert all(str(a.workspace_id) in step for step in steps)


def test_without_a_database_nothing_can_be_removed() -> None:
    c = client(None)
    assert c.get("/api/archived").json() == {"workspaces": [], "switches": False}
    assert post(c, "/api/workspaces/eva/archive").json() == {"detail": "workspaces.read_only"}
    assert post(c, "/api/archived/eva/restore").json() == {"detail": "workspaces.read_only"}


def test_a_page_from_elsewhere_is_refused() -> None:
    fake = FakeRemoval()
    c = client(fake)
    response = c.post("/api/workspaces/eva/archive", json={"credential": SIGNED})
    assert response.status_code == 403
    assert fake.calls == []


def test_a_database_that_does_not_answer_is_said_plainly() -> None:
    fake = FakeRemoval()
    fake.broken = True
    c = client(fake)
    assert c.get("/api/archived").json() == {"detail": "workspaces.store_unavailable"}
    assert post(c, "/api/archived/eva/restore").json() == {"detail": "workspaces.store_unavailable"}


def test_bringing_back_is_signed_for_the_one_named_in_the_address() -> None:
    fake = FakeRemoval()
    c = client(fake)
    post(c, "/api/workspaces/zaaheen/archive", {"credential": SIGNED})
    post(c, "/api/workspaces/eva/archive", {"credential": SIGNED})  # second in the archived list
    assert post(c, "/api/archived/eva/restore/options").json() == {"options": {"challenge": "c"}}
    assert fake.calls[-1] == ("options", purpose("restore", eva(fake)))


def test_without_a_passkey_set_up_no_signature_is_offered() -> None:
    fake = FakeRemoval()

    async def none_set_up(_step: str) -> dict[str, Any]:
        raise PasskeyError("registry.no_passkey")

    fake.options = none_set_up  # type: ignore[method-assign,assignment]
    c = client(fake)
    refused = post(c, "/api/workspaces/eva/archive/options")
    assert (refused.status_code, refused.json()) == (409, {"detail": "registry.no_passkey"})

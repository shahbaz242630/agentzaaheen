"""Adding a project from the Board: the token's repositories, then a name and one of them (F17 slice 5a)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from agentzaaheen.core.api import SamplePortfolio, create_app
from agentzaaheen.core.api.adding import RepoOption
from agentzaaheen.core.api.app import REQUEST_HEADER
from agentzaaheen.core.policy import Added, LifecycleVerdict, slug_of
from agentzaaheen.core.sdk import SourceError, SourceErrorCode
from agentzaaheen.core.store import StoreError, StoreErrorCode, WorkspaceRow

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
OURS = {REQUEST_HEADER: "1", "Origin": "http://127.0.0.1"}


class FakeAdding:
    """Adds workspaces to a list like the real one; the token covers ``covered``."""

    def __init__(self) -> None:
        self.live: list[WorkspaceRow] = []
        self.covered = ["owner/shop", "owner/app"]
        self.connected = True
        self.github_down = False
        self.store_down = False
        self.calls: list[tuple[str, str | None]] = []

    async def repos(self) -> list[RepoOption]:
        if self.github_down:
            raise SourceError(SourceErrorCode.UNAVAILABLE, "down")
        if self.store_down:
            raise StoreError(StoreErrorCode.UNAVAILABLE, "down")
        return [RepoOption(repo=r, used_by="shop" if r == "owner/shop" and self._has("shop") else None)
                for r in self.covered]  # fmt: skip

    async def add(self, name: str, repo: str | None) -> Added:
        self.calls.append((name, repo))
        if self.github_down:
            raise SourceError(SourceErrorCode.UNAVAILABLE, "down")
        if repo is not None and repo not in self.covered:
            return Added(LifecycleVerdict.REPO_NOT_COVERED)
        if self._has(slug_of(name)):
            return Added(LifecycleVerdict.NAME_TAKEN)
        row = WorkspaceRow(uuid4(), slug_of(name), name)
        self.live.append(row)
        return Added(LifecycleVerdict.DONE, row)

    async def active(self) -> list[WorkspaceRow]:
        if self.store_down:
            raise StoreError(StoreErrorCode.UNAVAILABLE, "down")
        return list(self.live)

    def _has(self, slug: str) -> bool:
        return any(w.slug == slug for w in self.live)


def client(adding: FakeAdding | None) -> TestClient:
    app = create_app(SamplePortfolio(NOW), adding=adding)
    if adding is not None:
        adding.live = list(app.state.workspaces.all())
    return TestClient(app, base_url="http://127.0.0.1")


def post(c: TestClient, body: object) -> httpx.Response:
    response: httpx.Response = c.post("/api/workspaces", json=body, headers=OURS)
    return response


def test_the_form_offers_the_token_s_repositories_and_says_which_are_taken() -> None:
    fake = FakeAdding()
    c = client(fake)
    post(c, {"name": "Shop", "repo": "owner/shop"})
    assert c.get("/api/github/repos").json() == {
        "repos": [{"repo": "owner/shop", "used_by": "shop"}, {"repo": "owner/app", "used_by": None}],
        "connected": True,
    }


def test_a_new_project_answers_at_its_address_at_once_and_joins_the_board() -> None:
    c = client(FakeAdding())
    assert c.get("/api/workspaces/shop").status_code == 404
    made = post(c, {"name": "Shop", "repo": "owner/shop"})
    assert (made.status_code, made.json()) == (201, {"slug": "shop", "name": "Shop"})
    assert c.get("/api/workspaces/shop").status_code == 200
    assert "shop" in [w["id"] for w in c.get("/api/workspaces").json()]


def test_a_refused_project_says_why_and_changes_nothing() -> None:
    fake = FakeAdding()
    c = client(fake)
    for body, why in [
        ({"name": "Shop", "repo": "owner/secret"}, "workspaces.repo_not_covered"),
        ({"name": "Zaaheen", "repo": None}, "workspaces.name_taken"),
    ]:
        refused = post(c, body)
        assert (refused.status_code, refused.json()) == (409, {"detail": why})
    assert c.get("/api/workspaces/shop").status_code == 404


def test_without_a_token_no_repositories_are_offered() -> None:
    fake = FakeAdding()
    fake.connected = False
    assert client(fake).get("/api/github/repos").json() == {"repos": [], "connected": False}
    assert client(None).get("/api/github/repos").json() == {"repos": [], "connected": False}


def test_without_a_database_nothing_can_be_added() -> None:
    refused = post(client(None), {"name": "Shop"})
    assert (refused.status_code, refused.json()) == (503, {"detail": "workspaces.read_only"})


def test_github_or_the_database_not_answering_is_said_plainly() -> None:
    fake = FakeAdding()
    c = client(fake)
    fake.github_down = True
    assert c.get("/api/github/repos").json() == {"detail": "source.unavailable"}
    assert post(c, {"name": "Shop", "repo": "owner/shop"}).json() == {"detail": "source.unavailable"}
    fake.github_down, fake.store_down = False, True
    assert c.get("/api/github/repos").json() == {"detail": "workspaces.store_unavailable"}
    assert post(c, {"name": "Shop"}).json() == {"detail": "workspaces.store_unavailable"}


def test_a_page_from_elsewhere_is_refused() -> None:
    fake = FakeAdding()
    response = client(fake).post("/api/workspaces", json={"name": "Shop"})
    assert response.status_code == 403
    assert fake.calls == []


def test_an_oversized_request_is_refused_before_the_rules() -> None:
    fake = FakeAdding()
    c = client(fake)
    assert post(c, {"name": "x" * 201}).status_code == 422
    assert post(c, {"name": "Shop", "extra": 1}).status_code == 422
    assert fake.calls == []

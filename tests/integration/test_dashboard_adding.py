"""Adding a project on the laptop, against a real database and a pretend GitHub (F17 slice 5a).

The laptop set-up saves each of its five projects' repositories once; the
Add a project form lists only the repositories the token covers, marks the
ones in use, and refuses one the token cannot read before anything is saved.
A project added over HTTP is still there after a restart.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agentzaaheen.core.api.adding import RepoOption
from agentzaaheen.core.policy import Added, LifecycleVerdict
from agentzaaheen.core.store import Database, DatabaseSettings, WorkspaceSources
from tests.integration.conftest import Server, run
from tests.integration.test_dashboard_switches import OURS, _switches, laptop_url, start, ui
from tools import laptop_db
from tools.dashboard_adding import LaptopAdding
from tools.dashboard_switches import LaptopSwitches

__all__ = ["laptop_url", "ui"]  # pytest fixtures shared with the switches tests

COVERED = [*laptop_db.LAPTOP_REPOS.values(), "shahbaz242630/Sage"]


def _github(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/user/repos" and request.headers.get("Authorization", "").startswith("Bearer "):
        return httpx.Response(200, json=[{"full_name": name} for name in COVERED])
    return httpx.Response(404)


def _adding(url: SecretStr, token: SecretStr | None) -> tuple[LaptopAdding, LaptopSwitches]:
    switches = _switches(url)
    return LaptopAdding(switches, token, httpx.MockTransport(_github)), switches


def test_the_laptop_set_up_saves_each_project_s_repository_once(
    laptop_url: SecretStr, server: Server
) -> None:
    run(laptop_db.set_up(server.admin_url, laptop_url.get_secret_value().rsplit("/", 1)[1]))  # twice is safe

    async def body() -> dict[str, str | None]:
        db = await Database.open(DatabaseSettings(dsn=laptop_url))
        try:
            return {
                w.slug: await WorkspaceSources(db.scoped(laptop_db.LAPTOP_TENANT, w.workspace_id)).github()
                for w in laptop_db.LAPTOP_WORKSPACES
            }
        finally:
            await db.close()

    assert run(body()) == laptop_db.LAPTOP_REPOS


def test_the_form_lists_the_token_s_repositories_and_who_reads_them(laptop_url: SecretStr) -> None:
    async def body() -> list[RepoOption]:
        adding, switches = _adding(laptop_url, SecretStr("github_pat_" + "r" * 30))
        try:
            return await adding.repos()
        finally:
            await switches.close()

    listed = {o.repo: o.used_by for o in run(body())}
    assert listed["shahbaz242630/Document-Vault"] == "sandoq-kin"
    assert listed["shahbaz242630/Sage"] is None
    assert len(listed) == 6


def test_a_repository_is_saved_as_github_spells_it_and_one_it_cannot_read_is_refused(
    laptop_url: SecretStr,
) -> None:
    async def body() -> list[object]:
        adding, switches = _adding(laptop_url, SecretStr("github_pat_" + "r" * 30))
        try:
            sage = await adding.add("Sage", "SHAHBAZ242630/sage")
            hidden = await adding.add("Hidden", "shahbaz242630/not-in-the-token")
            twice = await adding.add("Sandoq again", "shahbaz242630/Document-Vault")
            assert sage.workspace is not None
            repo = await switches.repos_in_use()
            return [sage.verdict, hidden.verdict, twice.verdict, repo.get("sage")]
        finally:
            await switches.close()

    assert run(body()) == [
        LifecycleVerdict.DONE,
        LifecycleVerdict.REPO_NOT_COVERED,
        LifecycleVerdict.REPO_TAKEN,
        "shahbaz242630/Sage",
    ]


def test_without_a_token_a_project_can_be_added_but_not_given_a_repository(laptop_url: SecretStr) -> None:
    async def body() -> list[Added]:
        adding, switches = _adding(laptop_url, None)
        try:
            return [await adding.add("Notes", None), await adding.add("Shop", "shahbaz242630/Sage")]
        finally:
            await switches.close()

    notes, shop = run(body())
    assert (notes.verdict, shop.verdict) == (LifecycleVerdict.DONE, LifecycleVerdict.REPO_NOT_COVERED)


def test_a_project_added_over_http_is_there_after_a_restart(ui: Path, laptop_url: SecretStr) -> None:
    with TestClient(start(ui, laptop_url), base_url="http://127.0.0.1") as client:
        assert client.get("/api/github/repos").json() == {"repos": [], "connected": False}  # no token given
        made = client.post("/api/workspaces", json={"name": "Notes"}, headers=OURS)
        assert (made.status_code, made.json()) == (201, {"slug": "notes", "name": "Notes"})
        assert client.get("/api/workspaces/notes").json()["project"]["summary"] == "No data yet"

    with TestClient(start(ui, laptop_url), base_url="http://127.0.0.1") as client:
        assert "notes" in [w["id"] for w in client.get("/api/workspaces").json()]
        again = client.post("/api/workspaces", json={"name": "notes"}, headers=OURS)
        assert (again.status_code, again.json()) == (409, {"detail": "workspaces.name_taken"})

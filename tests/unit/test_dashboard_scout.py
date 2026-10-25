"""The scout on the laptop: a run in the background, its stages, and every way it can end (F17 slice 5b-2)."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agentzaaheen.core.api import SamplePortfolio, create_app
from agentzaaheen.core.api.app import REQUEST_HEADER
from agentzaaheen.core.api.scout import Asked, scout_view
from agentzaaheen.core.policy import ScoutControl, ScoutVerdict
from agentzaaheen.core.registry.roles import RoleLibrary, RolePack
from agentzaaheen.core.sdk import SourceError, SourceErrorCode
from agentzaaheen.core.store import ScoutRun, StoreError, StoreErrorCode, WorkspaceRow
from tools.dashboard_profile import LaptopProfiles
from tools.dashboard_scout import CRASHED, LaptopScout

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
REPO = "owner/shop"
SHOP = WorkspaceRow(uuid4(), "shop", "Shop")
TOKEN = SecretStr("github_pat_" + "w" * 30)
OURS = {REQUEST_HEADER: "1", "Origin": "http://127.0.0.1"}
LIBRARY = RoleLibrary(
    packs=(
        RolePack("monitoring", "Monitoring", "Watches uptime.", "read", {}, "a" * 64),
        RolePack("marketing", "Marketing", "Finds buyers.", "draft", {}, "b" * 64),
    ),
    problems=(),
)
GOOD = json.dumps(
    {
        "summary": "A shop.",
        "roles": [
            {
                "role": "monitoring",
                "recommend": True,
                "reason": "It has CI.",
                "files": [".github/workflows/ci.yml"],
            },
            {"role": "marketing", "recommend": True, "reason": "README says so.", "files": ["README.md"]},
        ],
    }
)


def github(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    files = {"README.md": "# Shop", ".github/workflows/ci.yml": "on: push"}
    if path == f"/repos/{REPO}":
        return httpx.Response(200, json={"default_branch": "main", "pushed_at": "2026-10-24T08:00:00Z"})
    if path == f"/repos/{REPO}/languages":
        return httpx.Response(200, json={"TypeScript": 10})
    if path == f"/repos/{REPO}/git/trees/main":
        tree = [{"path": p, "type": "blob", "size": len(t)} for p, t in files.items()]
        return httpx.Response(200, json={"tree": tree, "truncated": False})
    if path.startswith(f"/repos/{REPO}/contents/"):
        text = files[path.rsplit("/contents/", 1)[1]]
        return httpx.Response(
            200, json={"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        )
    return httpx.Response(404)


class Model:
    """A pretend model: answers ``reply`` (or raises it), and keeps the messages it was sent."""

    def __init__(self, reply: str | Exception = GOOD) -> None:
        self.reply = reply
        self.sent: list[Sequence[Mapping[str, str]]] = []
        self.busy = 0
        self.most_at_once = 0

    async def chat(
        self, messages: Sequence[Mapping[str, str]], *, max_tokens: int, temperature: float
    ) -> str:
        self.sent.append(messages)
        self.busy += 1
        self.most_at_once = max(self.most_at_once, self.busy)
        await asyncio.sleep(0)
        self.busy -= 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class Control:
    """A pretend scout store with the real rules that matter here: one running run per workspace."""

    def __init__(self) -> None:
        self.runs: dict[UUID, dict[str, Any]] = {}
        self.stages: list[str] = []
        self.verdict: ScoutVerdict | None = None
        self.fail_store = False

    async def start(
        self, workspace: WorkspaceRow, *, repo: str, model: str, role: object, channel: object
    ) -> Any:
        if self.verdict is not None:
            return self.verdict, None
        if any(r["state"] == "running" and r["ws"] == workspace.slug for r in self.runs.values()):
            return ScoutVerdict.BUSY, None
        run_id = uuid4()
        self.runs[run_id] = {
            "ws": workspace.slug,
            "repo": repo,
            "model": model,
            "state": "running",
            "stage": "reading",
        }
        return ScoutVerdict.STARTED, run_id

    async def stage(
        self, workspace: WorkspaceRow, run_id: UUID, stage: str, *, branch: str | None = None
    ) -> None:
        self.stages.append(stage)
        self.runs[run_id]["stage"] = stage

    async def finish(self, workspace: WorkspaceRow, run_id: UUID, report: dict[str, Any]) -> bool:
        self.runs[run_id] |= {"state": "done", "stage": "finished", "result": report}
        return True

    async def fail(self, workspace: WorkspaceRow, run_id: UUID, reason: str) -> bool:
        if self.fail_store:
            raise StoreError(StoreErrorCode.UNAVAILABLE, "down")
        self.runs[run_id] |= {"state": "failed", "stage": "finished", "reason": reason}
        return True

    async def latest(self, workspace: WorkspaceRow) -> ScoutRun | None:
        for run_id, r in reversed(self.runs.items()):
            if r["ws"] == workspace.slug:
                return ScoutRun(
                    run_id=run_id, repo=r["repo"], branch="main", model=r["model"], state=r["state"],
                    stage=r["stage"], started_by="user:chairman", started_at=NOW,
                    finished_at=None if r["state"] == "running" else NOW, reason=r.get("reason"),
                    result=r.get("result"),
                )  # fmt: skip
        return None


def scout(
    control: Control,
    model: Model | None,
    repos: dict[str, str] | None = None,
    profiles: LaptopProfiles | None = None,
) -> LaptopScout:
    async def get() -> ScoutControl:
        return cast("ScoutControl", control)

    async def known() -> dict[str, str]:
        return {"shop": REPO, "zaaheen": REPO} if repos is None else repos

    return LaptopScout(
        control=get, repos=known, token=TOKEN, model=model, model_id="local-gemma", library=LIBRARY,
        clock=lambda: NOW, transport=httpx.MockTransport(github), profiles=profiles,
    )  # fmt: skip


def ask_and_wait(laptop: LaptopScout) -> Asked:
    async def body() -> Asked:
        asked = await laptop.ask(SHOP)
        await laptop.wait()
        return asked

    return asyncio.run(body())


def only(control: Control) -> dict[str, Any]:
    (run,) = control.runs.values()
    return run


def test_a_run_reads_thinks_checks_and_saves_the_checked_advice() -> None:
    control, model = Control(), Model()
    assert ask_and_wait(scout(control, model)) == "scout.started"
    run = only(control)
    assert control.stages == ["thinking", "checking"]
    assert run["state"] == "done"
    roles = {r["role_id"]: r for r in run["result"]["roles"]}
    assert roles["monitoring"]["recommend"] is True
    assert roles["marketing"]["recommend"] is False  # README alone: downgraded by code
    assert "<untrusted>" in model.sent[0][1]["content"]


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ("Sure! You need marketing.", "scout.not_json"),
        (SourceError(SourceErrorCode.UNAVAILABLE, "down"), "source.unavailable"),
        (RuntimeError("boom"), CRASHED),
    ],
)
def test_every_failure_closes_the_run_with_a_reason(reply: str | Exception, reason: str) -> None:
    control = Control()
    ask_and_wait(scout(control, Model(reply)))
    assert only(control) | {"result": None} == only(control) | {"result": None}
    assert (only(control)["state"], only(control)["reason"]) == ("failed", reason)


def test_a_github_failure_is_saved_at_the_reading_stage() -> None:
    control = Control()
    laptop = scout(control, Model(), repos={"shop": "owner/gone"})
    ask_and_wait(laptop)
    assert (only(control)["state"], only(control)["reason"]) == ("failed", "source.refused")
    assert control.stages == []


def test_a_database_that_is_down_at_the_end_still_ends_quietly() -> None:
    control = Control()
    control.fail_store = True
    ask_and_wait(scout(control, Model("not json")))  # no exception escapes the background task
    assert only(control)["state"] == "running"  # the next ask closes it as interrupted


def test_no_repository_means_no_run() -> None:
    control = Control()
    assert ask_and_wait(scout(control, Model(), repos={})) == "scout.no_repo"
    assert control.runs == {}


@pytest.mark.parametrize(
    ("verdict", "said"), [(ScoutVerdict.BUSY, "scout.busy"), (ScoutVerdict.NOT_ALLOWED, "scout.not_allowed")]
)
def test_a_refused_ask_starts_nothing(verdict: ScoutVerdict, said: Asked) -> None:
    control = Control()
    control.verdict = verdict
    assert ask_and_wait(scout(control, Model())) == said
    assert control.runs == {}


def test_runs_in_two_workspaces_take_turns_on_the_model() -> None:
    control, model = Control(), Model()
    laptop = scout(control, model, repos={"shop": REPO, "cafe": REPO})

    async def body() -> None:
        await laptop.ask(SHOP)
        await laptop.ask(WorkspaceRow(uuid4(), "cafe", "Cafe"))
        await laptop.wait()

    asyncio.run(body())
    assert [r["state"] for r in control.runs.values()] == ["done", "done"]
    assert model.most_at_once == 1


def test_stopping_the_dashboard_cancels_a_run_half_way() -> None:
    control = Control()

    class Slow(Model):
        async def chat(
            self, messages: Sequence[Mapping[str, str]], *, max_tokens: int, temperature: float
        ) -> str:
            await asyncio.sleep(60)
            return GOOD

    laptop = scout(control, Slow())

    async def body() -> None:
        await laptop.ask(SHOP)
        await asyncio.sleep(0.05)
        await laptop.close()

    asyncio.run(body())
    assert only(control)["state"] == "running"


def test_it_is_connected_only_with_a_token_a_model_and_roles() -> None:
    assert scout(Control(), Model()).connected
    assert not scout(Control(), None).connected


# ---- the routes


def client(laptop: LaptopScout | None) -> TestClient:
    return TestClient(create_app(SamplePortfolio(NOW), scout=laptop), base_url="http://127.0.0.1")


def test_ask_answers_at_once_and_get_shows_the_advice_later() -> None:
    control = Control()
    laptop = scout(control, Model())
    c = client(laptop)
    asked = c.post("/api/workspaces/zaaheen/scout", json={}, headers=OURS)
    assert asked.status_code == 202
    assert asked.json()["state"] in {"running", "done"}
    later = c.get("/api/workspaces/zaaheen/scout").json()
    assert later["connected"] is True
    assert later["repo"] == REPO


def test_routes_refuse_in_words() -> None:
    assert client(None).post("/api/workspaces/zaaheen/scout", json={}, headers=OURS).json() == {
        "detail": "scout.not_connected"
    }
    assert client(None).get("/api/workspaces/zaaheen/scout").json()["state"] == "none"
    control = Control()
    control.verdict = ScoutVerdict.BUSY
    busy = client(scout(control, Model())).post("/api/workspaces/zaaheen/scout", json={}, headers=OURS)
    assert (busy.status_code, busy.json()) == (409, {"detail": "scout.busy"})
    no_repo = client(scout(Control(), Model(), repos={})).post(
        "/api/workspaces/zaaheen/scout", json={}, headers=OURS
    )
    assert (no_repo.status_code, no_repo.json()) == (409, {"detail": "scout.no_repo"})
    foreign = client(scout(Control(), Model())).post("/api/workspaces/zaaheen/scout", json={})
    assert foreign.status_code == 403
    assert (
        client(scout(Control(), Model()))
        .post("/api/workspaces/nope/scout", json={}, headers=OURS)
        .status_code
        == 404
    )


def test_a_database_that_is_down_is_said_in_words() -> None:
    class Down(Control):
        async def latest(self, workspace: WorkspaceRow) -> ScoutRun | None:
            raise StoreError(StoreErrorCode.UNAVAILABLE, "down")

        async def start(self, *args: object, **kwargs: object) -> Any:
            raise StoreError(StoreErrorCode.UNAVAILABLE, "down")

    c = client(scout(Down(), Model()))
    assert c.get("/api/workspaces/zaaheen/scout").json() == {"detail": "scout.store_unavailable"}
    assert c.post("/api/workspaces/zaaheen/scout", json={}, headers=OURS).json() == {
        "detail": "scout.store_unavailable"
    }


def test_saved_advice_that_no_longer_reads_as_a_report_shows_as_failed() -> None:
    run = ScoutRun(
        run_id=uuid4(), repo=REPO, branch="main", model="local-gemma", state="done", stage="finished",
        started_by="user:chairman", started_at=NOW, finished_at=NOW, reason=None, result={"old": "shape"},
    )  # fmt: skip
    view = scout_view(run, connected=True)
    assert (view.state, view.reason, view.report) == ("failed", "scout.unreadable", None)


def test_a_workspace_never_scouted_shows_none() -> None:
    assert scout_view(None, connected=False).state == "none"


def test_what_the_project_is_built_with_reaches_the_model_as_facts() -> None:
    control, model = Control(), Model()

    async def known() -> dict[str, str]:
        return {"shop": REPO}

    profiles = LaptopProfiles(TOKEN, known, lambda: NOW, httpx.MockTransport(github))
    laptop = scout(control, model, profiles=profiles)
    ask_and_wait(laptop)
    assert "Languages (counted by GitHub): TypeScript 100.0%." in model.sent[0][1]["content"]
    assert only(control)["state"] == "done"


def test_the_scout_still_runs_when_the_profile_cannot_be_read() -> None:
    control, model = Control(), Model()

    async def known() -> dict[str, str]:
        return {"shop": "owner/gone"}  # the profile reader finds nothing there

    laptop = scout(
        control, model, profiles=LaptopProfiles(TOKEN, known, lambda: NOW, httpx.MockTransport(github))
    )
    ask_and_wait(laptop)
    assert "Languages" not in model.sent[0][1]["content"]
    assert only(control)["state"] == "done"

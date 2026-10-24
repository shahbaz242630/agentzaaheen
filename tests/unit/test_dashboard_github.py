"""The dashboard's GitHub mode: a pretend GitHub, never a real request, and never the token on screen."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.fixtures import github_world as github
from tests.fixtures import paddle_world as paddle
from tools import dashboard, dashboard_sources
from yaqzan.core.api import SamplePortfolio

FAKE_TOKEN = "github_pat_" + "q" * 30  # built here, never a real token
FAKE_KEY = "pdl_live_apikey_" + "q" * 20
REPOS = {"zaaheen": github.REPO, "eva": "acme-labs/missing"}


class Clock:
    def __init__(self) -> None:
        self.now = github.NOW + timedelta(minutes=1)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(dashboard_sources, "GITHUB_REPOS", REPOS)
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return tmp_path


def start(
    ui: Path, argv: list[str], serve: httpx.MockTransport, environ: dict[str, str], clock: Clock
) -> tuple[int, list[FastAPI]]:
    apps: list[FastAPI] = []
    code = dashboard.main(
        argv,
        static_dir=ui,
        serve=lambda app, _h, _p: apps.append(app),
        environ=environ,
        transport=serve,
        clock=clock,
    )
    return code, apps


def code_card(app: FastAPI, project: str) -> dict[str, object]:
    detail = TestClient(app, base_url="http://127.0.0.1").get(f"/api/workspaces/{project}").json()
    return next(card for card in detail["cards"] if card["id"] == "github")


def test_each_project_gets_its_real_code_card_and_the_token_never_shows(
    ui: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = github.GitHubWorld()
    code, apps = start(
        ui, ["--github-token-env", "T"], httpx.MockTransport(world.serve), {"T": FAKE_TOKEN}, Clock()
    )
    assert code == 0
    out = capsys.readouterr().out
    assert f"{github.REPO}: 5 open issues, 2 open pull requests, build passed." in out
    assert "real numbers from GitHub for every project" in out
    assert FAKE_TOKEN not in out
    assert {r.method for r in world.requests} == {"GET"}
    assert {r.url.host for r in world.requests} == {"api.github.com"}

    card = code_card(apps[0], "zaaheen")
    assert (card["sample"], card["freshness"], card["source"]) == (False, "fresh", "GitHub (live)")
    values = {m["label"]: m["value"] for m in card["metrics"]}  # type: ignore[attr-defined]
    assert values["Open issues"] == {"kind": "count", "value": 5}
    assert values["Failing workflows"] == {"kind": "count", "value": 0}


def test_a_repository_that_cannot_be_read_says_why_and_the_rest_still_show(
    ui: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, apps = start(
        ui,
        ["--github-token-env", "T"],
        httpx.MockTransport(github.GitHubWorld().serve),
        {"T": FAKE_TOKEN},
        Clock(),
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "acme-labs/missing could not be read (net.client_error at GET /repos/acme-labs/missing)." in out
    assert "What to do:" in out
    eva = code_card(apps[0], "eva")
    assert eva["freshness"] == "never"
    assert all(m["value"] == {"kind": "unknown", "reason": "net.client_error"} for m in eva["metrics"])  # type: ignore[attr-defined]
    assert code_card(apps[0], "zaaheen")["freshness"] == "fresh"


def test_the_code_card_turns_unknown_once_the_read_is_old(ui: Path) -> None:
    clock = Clock()
    _, apps = start(
        ui,
        ["--github-token-env", "T"],
        httpx.MockTransport(github.GitHubWorld().serve),
        {"T": FAKE_TOKEN},
        clock,
    )
    clock.now = github.NOW + dashboard_sources.GITHUB_SCHEDULE * 3
    card = code_card(apps[0], "zaaheen")
    assert card["freshness"] == "stale"
    assert all(m["value"]["kind"] == "unknown" for m in card["metrics"])  # type: ignore[attr-defined]


def test_paddle_and_github_together(ui: Path, capsys: pytest.CaptureFixture[str]) -> None:
    gh, pd = github.GitHubWorld(), paddle.PaddleWorld()

    def serve(request: httpx.Request) -> httpx.Response:
        return gh.serve(request) if request.url.host == "api.github.com" else pd.serve(request)

    code, apps = start(
        ui,
        ["--paddle-key-env", "K", "--github-token-env", "T"],
        httpx.MockTransport(serve),
        {"K": FAKE_KEY, "T": FAKE_TOKEN},
        Clock(),
    )
    assert code == 0
    assert "real numbers from Paddle for Zaaheen and GitHub for every project" in capsys.readouterr().out
    detail = TestClient(apps[0], base_url="http://127.0.0.1").get("/api/workspaces/zaaheen").json()
    real = {card["id"] for card in detail["cards"] if card["sample"] is False}
    assert real == {"paddle", "github"}


@pytest.mark.parametrize(
    ("environ", "words"),
    [({}, "T is not set"), ({"T": "github pat"}, "spaces or control characters")],
)
def test_a_missing_or_mangled_token_is_refused_before_any_request(
    ui: Path, environ: dict[str, str], words: str, capsys: pytest.CaptureFixture[str]
) -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent")

    code, apps = start(ui, ["--github-token-env", "T"], httpx.MockTransport(serve), environ, Clock())
    assert (code, apps) == (1, [])
    assert words in capsys.readouterr().out


def test_every_project_has_a_repository_and_a_code_card_to_fill() -> None:
    sample = SamplePortfolio(github.NOW)
    assert set(dashboard_sources.GITHUB_REPOS) == {p.id for p in sample.projects()}
    for project in dashboard_sources.GITHUB_REPOS:
        detail = sample.project(project)
        assert detail is not None
        assert any(card.id == "github" for card in detail.cards)

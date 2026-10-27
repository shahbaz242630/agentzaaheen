"""Paddle, GitHub and Supabase read again every hour, so the 05:00 summaries never see a stale card.

Found 27 Oct 2026: each was read only at start, so 6 hours later every card was
"unknown" and the server's briefing said "Could not read Revenue, Code, Backend".
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentzaaheen.core.api import SamplePortfolio
from agentzaaheen.core.sdk import SourceError, SourceErrorCode
from tests.fixtures import github_world as github
from tests.fixtures import paddle_world as paddle
from tools import dashboard, dashboard_sources
from tools.dashboard_reread import REREAD, HourlyReads, Reread
from tools.dashboard_sources import github_portfolio, live_portfolio

FAKE_TOKEN = "github_pat_" + "q" * 30  # built here, never a real token
FAKE_KEY = "pdl_live_apikey_" + "q" * 20
REPOS = {"zaaheen": github.REPO, "eva": "acme-labs/missing"}


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def card(app: FastAPI, project: str, card_id: str) -> dict[str, Any]:
    detail = TestClient(app, base_url="http://127.0.0.1").get(f"/api/workspaces/{project}").json()
    found: dict[str, Any] = next(c for c in detail["cards"] if c["id"] == card_id)
    return found


def lines(capsys: pytest.CaptureFixture[str], event: str) -> list[dict[str, Any]]:
    """The JSON log lines written to stderr for ``event``."""
    out = []
    for raw in capsys.readouterr().err.splitlines():
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if line.get("event") == event:
            out.append(line)
    return out


def test_the_github_card_is_fresh_again_after_a_reread(capsys: pytest.CaptureFixture[str]) -> None:
    clock = Clock(github.NOW + timedelta(minutes=1))
    world = github.GitHubWorld()
    reads: list[Reread] = []
    portfolio = github_portfolio(
        SamplePortfolio(clock()),
        "T",
        {"T": FAKE_TOKEN},
        clock=clock,
        transport=httpx.MockTransport(world.serve),
        repos=REPOS,
        reads=reads,
    )
    assert portfolio is not None
    assert [r.source for r in reads] == ["GitHub"]
    clock.now += timedelta(hours=7)  # the 05:00 summaries, hours after the start
    zaaheen = portfolio.project("zaaheen")
    assert zaaheen is not None
    assert next(c for c in zaaheen.cards if c.id == "github").freshness.value == "alert"
    asked = len(world.requests)
    capsys.readouterr()

    asyncio.run(HourlyReads(reads).round())

    assert len(world.requests) > asked  # it really asked GitHub again
    zaaheen = portfolio.project("zaaheen")
    assert zaaheen is not None
    assert next(c for c in zaaheen.cards if c.id == "github").freshness.value == "fresh"
    (done,) = lines(capsys, "reread_done")
    assert (done["source"], done["failing"]) == ("GitHub", 1)  # acme-labs/missing still cannot be read
    assert FAKE_TOKEN not in json.dumps(done)


def test_a_paddle_reread_that_fails_keeps_the_last_read(capsys: pytest.CaptureFixture[str]) -> None:
    clock = Clock(paddle.NOW)
    world = paddle.PaddleWorld()
    up = {"ok": True}

    def serve(request: httpx.Request) -> httpx.Response:
        if up["ok"]:
            return world.serve(request)
        return httpx.Response(401, json={"error": {"code": "forbidden"}})

    reads: list[Reread] = []
    portfolio = live_portfolio(
        "K", {"K": FAKE_KEY}, clock=clock, transport=httpx.MockTransport(serve), reads=reads
    )
    assert portfolio is not None
    capsys.readouterr()
    up["ok"] = False
    clock.now += timedelta(minutes=30)

    asyncio.run(HourlyReads(reads).round())

    (failed,) = lines(capsys, "reread_failed")
    assert failed["source"] == "Paddle"
    assert failed["level"] == "warning"
    assert "keep the last read" in failed["what"]
    assert FAKE_KEY not in json.dumps(failed)
    zaaheen = portfolio.project("zaaheen")
    assert zaaheen is not None
    revenue = next(c for c in zaaheen.cards if c.id == "paddle")
    assert revenue.freshness.value == "fresh"  # still the read from 30 minutes ago, honestly dated
    clock.now += timedelta(hours=3)
    zaaheen = portfolio.project("zaaheen")
    assert zaaheen is not None
    stale = next(c for c in zaaheen.cards if c.id == "paddle")
    assert stale.freshness.value != "fresh"  # never an old number shown as new

    up["ok"] = True
    asyncio.run(HourlyReads(reads).round())
    zaaheen = portfolio.project("zaaheen")
    assert zaaheen is not None
    assert next(c for c in zaaheen.cards if c.id == "paddle").freshness.value == "fresh"


def test_one_source_that_breaks_never_stops_the_others(capsys: pytest.CaptureFixture[str]) -> None:
    done: list[str] = []

    async def odd() -> int:
        raise RuntimeError("something odd")

    async def refused() -> int:
        raise SourceError(SourceErrorCode.AUTH, "refused")

    async def fine() -> int:
        done.append("fine")
        return 0

    reads = [Reread("Paddle", odd), Reread("GitHub", refused), Reread("Supabase", fine)]
    asyncio.run(HourlyReads(reads).round())
    assert done == ["fine"]
    err = capsys.readouterr().err
    events = [json.loads(raw) for raw in err.splitlines() if raw.startswith("{")]
    said = {(e["event"], e["source"]) for e in events if e.get("event", "").startswith("reread_")}
    assert said == {("reread_crashed", "Paddle"), ("reread_failed", "GitHub"), ("reread_done", "Supabase")}
    assert "something odd" not in err  # the kind of error, never its message


def test_the_background_thread_rereads_and_stops() -> None:
    rounds: list[int] = []

    async def count() -> int:
        rounds.append(1)
        return 0

    hourly = HourlyReads([Reread("GitHub", count)], interval_s=0.01)
    thread = hourly.start()
    deadline = datetime.now(UTC) + timedelta(seconds=5)
    while len(rounds) < 2 and datetime.now(UTC) < deadline:  # a plain thread, polled briefly
        thread.join(0.01)
    hourly.stop()
    thread.join(2)
    assert len(rounds) >= 2
    assert not thread.is_alive()


def test_the_reread_matches_the_cards_promise() -> None:
    # Every card turns "stale" after two schedules; one read an hour keeps them fresh.
    assert dashboard_sources.PADDLE_SCHEDULE == REREAD
    assert dashboard_sources.GITHUB_SCHEDULE == REREAD


def test_the_dashboard_says_what_it_reads_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(dashboard_sources, "GITHUB_REPOS", REPOS)
    started: list[HourlyReads] = []
    monkeypatch.setattr(HourlyReads, "start", lambda self: started.append(self))  # noqa: PLW0108  # gets self
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    gh, pd = github.GitHubWorld(), paddle.PaddleWorld()

    def serve(request: httpx.Request) -> httpx.Response:
        return gh.serve(request) if request.url.host == "api.github.com" else pd.serve(request)

    apps: list[FastAPI] = []
    clock = Clock(github.NOW + timedelta(minutes=1))
    code = dashboard.main(
        ["--paddle-key-env", "K", "--github-token-env", "T"],
        static_dir=tmp_path,
        serve=lambda app, _h, _p: apps.append(app),
        environ={"K": FAKE_KEY, "T": FAKE_TOKEN},
        transport=httpx.MockTransport(serve),
        clock=clock,
    )
    assert code == 0
    assert "dashboard: Paddle, GitHub will be read again every hour." in capsys.readouterr().out
    assert len(started) == 1
    clock.now += timedelta(hours=7)
    assert card(apps[0], "zaaheen", "github")["freshness"] == "alert"
    asyncio.run(started[0].round())
    assert card(apps[0], "zaaheen", "github")["freshness"] == "fresh"

"""The morning job's harder paths, with an in-memory store (F17 slice 6).

The happy path runs against a real Postgres in ``tests/integration``. Here:
what the summary says when the agents or the scout cannot be read, each state
the scout can be in, a save that fails (tried again later, never guessed), and
a check that crashes (the job carries on).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr

from agentzaaheen.core.api import SamplePortfolio
from agentzaaheen.core.api.agents import AgentOut, WorkspaceAgentsOut
from agentzaaheen.core.api.summary import AgentsFact, ScoutFact, read_summary
from agentzaaheen.core.api.workspaces import WorkspaceDirectory
from agentzaaheen.core.notify import Alert
from agentzaaheen.core.policy import SummaryControl
from agentzaaheen.core.store import (
    BriefingRow,
    ScoutRun,
    StoreError,
    StoreErrorCode,
    SummaryRow,
    WorkspaceRow,
)
from tools.dashboard import _briefing_ping
from tools.dashboard_summaries import AlertLog, LaptopSummaries

DUBAI = ZoneInfo("Asia/Dubai")
NOW = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)  # 05:00 in Dubai
EVA = WorkspaceRow(uuid4(), "eva", "Eva")
REPORT = {
    "summary": "An app.",
    "roles": [
        {"role_id": "monitoring", "title": "Monitoring", "recommend": True, "reason": "It runs.",
         "files": [], "removed_files": 0, "downgraded": False},
        {"role_id": "sales", "title": "Sales", "recommend": False, "reason": "No shop.",
         "files": [], "removed_files": 0, "downgraded": False},
    ],
    "not_reviewed": [], "suggested_providers": [], "notes": [],
}  # fmt: skip


class Store:
    """An in-memory stand-in for ``SummaryControl``; ``fail_save`` makes saving fail."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, date], SummaryRow] = {}
        self.fail_save = False
        self.calls = 0

    async def save(self, workspace: WorkspaceRow, day: date, facts: dict[str, Any]) -> None:
        self.calls += 1
        if self.fail_save:
            raise StoreError(StoreErrorCode.UNAVAILABLE, "down")
        self.rows[(workspace.slug, day)] = SummaryRow(day, NOW, facts)

    async def recent(self, workspace: WorkspaceRow, *, until: date, days: int = 7) -> list[SummaryRow]:
        mine = sorted((r for (s, d), r in self.rows.items() if s == workspace.slug and d <= until),
                      key=lambda r: r.day, reverse=True)  # fmt: skip
        return mine[:days]

    async def before(self, workspace: WorkspaceRow, day: date) -> SummaryRow | None:
        found = await self.recent(workspace, until=day - timedelta(days=1), days=1)
        return found[0] if found else None

    async def briefings(self, *, until: date, days: int = 7) -> list[BriefingRow]:
        return []


def job(store: Store, **more: Any) -> LaptopSummaries:  # the job's optional parts
    async def opened() -> SummaryControl:
        return store  # type: ignore[return-value]

    return LaptopSummaries(
        control=opened,
        portfolio=SamplePortfolio(NOW),
        directory=WorkspaceDirectory([EVA]),
        clock=lambda: NOW,
        zone=DUBAI,
        **more,
    )


def made(store: Store) -> Any:  # the saved summary, read back
    summary = read_summary(store.rows[("eva", NOW.date())].facts)
    assert summary is not None
    return summary


def run_(store: Store, **more: Any) -> Any:
    asyncio.run(job(store, **more).make_now())
    return made(store)


def test_agents_and_scout_that_cannot_be_read_are_listed_first() -> None:
    async def down(_w: WorkspaceRow) -> Any:
        raise StoreError(StoreErrorCode.UNAVAILABLE, "down")

    summary = run_(Store(), agents=down, scout=down)
    assert [(u.title, u.reason) for u in summary.unread][-2:] == [
        ("Agents", "store.unavailable"), ("Scout", "store.unavailable"),
    ]  # fmt: skip
    assert (summary.agents, summary.scout) == (None, None)


def test_agents_are_counted() -> None:
    agent = AgentOut(id=uuid4(), role_id="monitoring", title="Monitoring", enabled=True, model="local-gemma",
                     cap_minor=500, spent_minor=40, newer_version=False)  # fmt: skip

    async def view(_w: WorkspaceRow) -> WorkspaceAgentsOut:
        return WorkspaceAgentsOut(enabled=True, cap_minor=900, spent_minor=40, agents=(agent,), switches=True)

    assert run_(Store(), agents=view).agents == AgentsFact(
        agents=1, on=1, workspace_on=True, spent_minor=40, cap_minor=900
    )


def scout_run(state: str, result: dict[str, Any] | None = None, reason: str | None = None) -> ScoutRun:
    return ScoutRun(
        run_id=uuid4(),
        repo="owner/eva",
        branch="main",
        model="local-gemma",
        state=state,  # type: ignore[arg-type]
        stage="finished",
        started_by="user:chairman",
        started_at=NOW - timedelta(hours=1),
        finished_at=None if state == "running" else NOW,
        reason=reason,
        result=result,
    )


def test_each_scout_state_is_said() -> None:
    def latest(found: ScoutRun | None) -> Any:
        async def read(_w: WorkspaceRow) -> ScoutRun | None:
            return found

        return read

    assert run_(Store(), scout=latest(None)).scout is None
    running = run_(Store(), scout=latest(scout_run("running")))
    assert running.scout == ScoutFact(state="running", at=NOW - timedelta(hours=1))
    failed = run_(Store(), scout=latest(scout_run("failed", reason="scout.not_json")))
    assert (failed.scout.state, failed.scout.reason) == ("failed", "scout.not_json")
    done = run_(Store(), scout=latest(scout_run("done", REPORT)))
    assert done.scout.recommended == ("monitoring",)
    broken = run_(Store(), scout=latest(scout_run("done", {"summary": 3})))
    assert broken.scout is None
    assert ("Scout", "scout.unreadable") in [(u.title, u.reason) for u in broken.unread]


def test_a_save_that_fails_is_tried_again_after_a_wait() -> None:
    store = Store()
    store.fail_save = True
    summaries = job(store)
    assert asyncio.run(summaries.tick()) is not None
    assert store.calls == 1
    assert asyncio.run(summaries.tick()) is None  # waiting before the next try
    assert store.calls == 1
    assert store.rows == {}


def test_only_watched_projects_count_alerts() -> None:
    log = AlertLog(lambda: NOW - timedelta(hours=2))
    log.add([Alert("eva", True, "t", "m", "high"), Alert("eva", False, "t", "m", "default")])
    assert run_(Store(), alerts=log, watched=frozenset({"eva"})).alerts.closed == 1
    assert run_(Store(), alerts=log).alerts is None


def test_a_check_that_crashes_does_not_end_the_job() -> None:
    async def body() -> int:
        crashes = 0

        async def broken() -> SummaryControl:
            nonlocal crashes
            crashes += 1
            raise RuntimeError("boom")

        summaries = LaptopSummaries(control=broken, portfolio=SamplePortfolio(NOW),
                                    directory=WorkspaceDirectory([EVA]), clock=lambda: NOW, zone=DUBAI,
                                    check_every_s=0.01)  # fmt: skip
        await summaries.close()  # closing before starting is fine
        await summaries.start()
        await asyncio.sleep(0.1)
        await summaries.close()
        return crashes

    assert asyncio.run(body()) >= 2  # it kept checking after the first crash


PING = "https://hc-ping.com/0f3c6a1e-5b7d-4c2a-9e8f-1a2b3c4d5e6f"  # made up
TOPIC = SecretStr("abcdefghijklmnop" + "qrstuvwx")


def test_the_briefing_ping_address_is_checked_at_start(capsys: pytest.CaptureFixture[str]) -> None:
    env = {"PING": PING, "ODD": "https://evil.example/x", "BLANK": " "}
    assert _briefing_ping(env, None, TOPIC) is None  # not asked for: nothing said
    found = _briefing_ping(env, "PING", TOPIC)
    assert found is not None
    assert found.get_secret_value() == PING
    assert capsys.readouterr().out == ""
    assert _briefing_ping(env, "BLANK", TOPIC) is None
    assert "BLANK is not set" in capsys.readouterr().out
    assert _briefing_ping(env, "ODD", TOPIC) is None
    said = capsys.readouterr().out
    assert "ODD is not a Healthchecks ping address" in said
    assert "evil" not in said  # the value is never shown
    assert _briefing_ping(env, "PING", None) is not None
    assert "Healthchecks will alert every morning" in capsys.readouterr().out

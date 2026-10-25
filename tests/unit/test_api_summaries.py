"""The summary routes: the Board's lines, a workspace's days, the briefings, and "make now" (F17 slice 6).

Without a database every route says it is not connected; a saved summary that
no longer reads as one is shown as unreadable, never guessed; the workspace
comes from the address; "make now" answers only our own page.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient

from yaqzan.core.api import SamplePortfolio, create_app
from yaqzan.core.api.app import REQUEST_HEADER
from yaqzan.core.api.summary import summarise
from yaqzan.core.store import BriefingRow, StoreError, StoreErrorCode, SummaryRow, WorkspaceRow

NOW = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)
DAY = date(2026, 10, 25)
OURS = {REQUEST_HEADER: "1", "Origin": "http://127.0.0.1"}
GOOD = summarise("zaaheen", "Zaaheen", None, day=DAY, made_at=NOW, window_start=NOW - timedelta(days=1))


class Backend:
    def __init__(self, *, down: bool = False) -> None:
        self.down = down
        self.asked: list[str] = []
        self.made = 0

    def _check(self) -> None:
        if self.down:
            raise StoreError(StoreErrorCode.UNAVAILABLE, "down")

    async def newest(self) -> Mapping[str, SummaryRow]:
        self._check()
        return {
            "zaaheen": SummaryRow(DAY, NOW, GOOD.model_dump(mode="json")),
            "eva": SummaryRow(DAY, NOW, {"version": 99}),
        }

    async def recent(self, workspace: WorkspaceRow) -> Sequence[SummaryRow]:
        self._check()
        self.asked.append(workspace.slug)
        return [
            SummaryRow(DAY, NOW, GOOD.model_dump(mode="json")),
            SummaryRow(DAY - timedelta(days=1), NOW, {}),
        ]

    async def briefings(self) -> Sequence[BriefingRow]:
        self._check()
        return [
            BriefingRow(DAY, NOW, "Yaqzan briefing", "1 project.\nDetails.", "not_sent", None, "net.timeout")
        ]

    async def make_now(self) -> tuple[int, int]:
        self._check()
        self.made += 1
        return 5, 0


def client(backend: Backend | None) -> TestClient:
    return TestClient(create_app(SamplePortfolio(NOW), summaries=backend), base_url="http://127.0.0.1")


def test_without_a_database_every_route_says_not_connected() -> None:
    c = client(None)
    assert c.get("/api/summaries").json() == {"connected": False, "workspaces": {}}
    assert c.get("/api/workspaces/zaaheen/summaries").json() == {"connected": False, "days": []}
    assert c.get("/api/briefings").json() == {"connected": False, "briefings": []}
    made = c.post("/api/summaries/make", json={}, headers=OURS)
    assert (made.status_code, made.json()) == (503, {"detail": "summaries.not_connected"})


def test_the_board_gets_one_line_each_and_an_unreadable_one_says_so() -> None:
    found = client(Backend()).get("/api/summaries").json()
    assert found["connected"] is True
    assert found["workspaces"]["zaaheen"]["line"] == ("Nothing to report: no real source is connected yet.")
    assert found["workspaces"]["eva"] == {
        "day": "2026-10-25", "made_at": "2026-10-25T03:00:00Z", "health": "unknown", "line": None,
    }  # fmt: skip


def test_a_workspace_gets_its_days_from_the_address() -> None:
    backend = Backend()
    days = client(backend).get("/api/workspaces/eva/summaries").json()["days"]
    assert backend.asked == ["eva"]
    assert days[0]["summary"]["workspace"] == "zaaheen"  # the facts as saved
    assert (days[1]["summary"], days[1]["line"]) == (None, None)  # unreadable, never guessed
    assert client(backend).get("/api/workspaces/nope/summaries").status_code == 404


def test_the_briefings_show_the_exact_words_and_whether_they_went() -> None:
    (row,) = client(Backend()).get("/api/briefings").json()["briefings"]
    assert (row["message"], row["state"], row["reason"]) == (
        "1 project.\nDetails.",
        "not_sent",
        "net.timeout",
    )


def test_make_now_answers_only_our_page() -> None:
    backend = Backend()
    assert client(backend).post("/api/summaries/make", json={}).status_code == 403
    assert backend.made == 0
    made = client(backend).post("/api/summaries/make", json={}, headers=OURS)
    assert (made.status_code, made.json()) == (200, {"made": 5, "failed": 0})


def test_a_database_that_is_down_is_said_in_words() -> None:
    c = client(Backend(down=True))
    for answer in (
        c.get("/api/summaries"),
        c.get("/api/workspaces/zaaheen/summaries"),
        c.get("/api/briefings"),
        c.post("/api/summaries/make", json={}, headers=OURS),
    ):
        assert (answer.status_code, answer.json()) == (503, {"detail": "summaries.store_unavailable"})

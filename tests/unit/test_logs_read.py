"""Reading the kept log lines back (ADR-034 slice 3): newest first, filtered, bounded, only safe fields."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yaqzan.core.api import SamplePortfolio, create_app
from yaqzan.core.telemetry import LogFolder, LogPage, LogQuery, logread
from yaqzan.core.telemetry.logfile import file_name
from yaqzan.core.telemetry.logread import read_logs

TODAY = date(2026, 10, 25)
NOW = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)
TRACE = "a" * 32


def line(n: int, **more: object) -> dict[str, object]:
    return {
        "timestamp": f"2026-10-25T09:{n:02d}:00Z",
        "level": "info",
        "event": "push_sent",
        "component": "yaqzan.push",
        "what": f"Line {n}.",
        "why": "Because.",
        "next": "",
        "bytes": n,
        **more,
    }


def write(folder: Path, day: date, lines: list[dict[str, object]], *, junk: bool = False) -> None:
    text = "".join(json.dumps(x) + "\n" for x in lines)
    if junk:
        text = "not json\n" + text + '{"cut short'
    (folder / file_name(day)).write_text(text, encoding="utf-8")


def read(folder: Path, **asked: object) -> LogPage:
    return read_logs(folder, LogQuery(**asked), TODAY)  # type: ignore[arg-type]


def test_newest_first_across_days(tmp_path: Path) -> None:
    write(tmp_path, TODAY - timedelta(days=1), [line(1), line(2)])
    write(tmp_path, TODAY, [line(3), line(4)])
    page = read(tmp_path, days=2)
    assert [x["bytes"] for x in page.lines] == [4, 3, 2, 1]
    assert not page.more


def test_only_today_unless_more_days_are_asked(tmp_path: Path) -> None:
    write(tmp_path, TODAY - timedelta(days=1), [line(1)])
    write(tmp_path, TODAY, [line(2)])
    assert [x["bytes"] for x in read(tmp_path).lines] == [2]


def test_the_limit_keeps_the_newest_and_says_there_is_more(tmp_path: Path) -> None:
    write(tmp_path, TODAY - timedelta(days=1), [line(n) for n in range(5)])
    write(tmp_path, TODAY, [line(n) for n in range(10, 13)])
    page = read(tmp_path, days=2, limit=4)
    assert [x["bytes"] for x in page.lines] == [12, 11, 10, 4]
    assert page.more
    exactly = read(tmp_path, days=2, limit=8)
    assert len(exactly.lines) == 8
    assert not exactly.more


def test_level_means_this_level_and_worse(tmp_path: Path) -> None:
    write(
        tmp_path, TODAY, [line(1), line(2, level="warning"), line(3, level="error"), line(4, level="debug")]
    )
    assert [x["bytes"] for x in read(tmp_path, level="warning").lines] == [3, 2]
    assert [x["bytes"] for x in read(tmp_path, level="error").lines] == [3]
    assert [x["bytes"] for x in read(tmp_path).lines] == [3, 2, 1]


@pytest.mark.parametrize(
    ("asked", "found"),
    [
        ({"project": "zaaheen"}, [3, 2, 1]),
        ({"agent": "a1"}, [4]),
        ({"trace": TRACE}, [5]),
        ({"event": "alert_sent"}, [6]),
    ],
)
def test_filters(tmp_path: Path, asked: dict[str, str], found: list[int]) -> None:
    write(
        tmp_path,
        TODAY,
        [
            line(1, project="zaaheen"),
            line(2, project_id="zaaheen"),
            line(3, workspace="zaaheen"),
            line(4, agent_id="a1"),
            line(5, trace_id=TRACE),
            line(6, event="alert_sent"),
            line(7, project="eva"),
        ],
    )
    assert [x["bytes"] for x in read(tmp_path, **asked).lines] == found


def test_only_fields_the_log_store_may_receive_are_shown(tmp_path: Path) -> None:
    write(
        tmp_path,
        TODAY,
        [line(1, exception="Traceback: /home/secret/path", arguments={"q": "x"}, path="/etc", chars=9)],
    )
    [shown] = read(tmp_path).lines
    assert "exception" not in shown
    assert "arguments" not in shown
    assert "path" not in shown
    assert shown["chars"] == 9
    assert shown["what"] == "Line 1."


def test_a_long_value_is_cut_and_a_nested_one_dropped(tmp_path: Path) -> None:
    write(tmp_path, TODAY, [line(1, reason="x" * 1_000, code={"nested": 1})])
    [shown] = read(tmp_path).lines
    assert shown["reason"] == "x" * logread.MAX_VALUE_CHARS
    assert "code" not in shown


def test_broken_lines_and_lines_without_an_event_are_skipped(tmp_path: Path) -> None:
    write(tmp_path, TODAY, [line(1), {"level": "info", "what": "no event"}, ["a list"], line(2)], junk=True)  # type: ignore[list-item]
    assert [x["bytes"] for x in read(tmp_path).lines] == [2, 1]


def test_a_huge_day_is_read_from_its_newest_end_and_says_it_was_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logread, "MAX_SCAN_BYTES", 600)
    write(tmp_path, TODAY, [line(n) for n in range(20)])
    page = read(tmp_path)
    assert page.cut
    assert page.lines[0]["bytes"] == 19
    assert 0 < len(page.lines) < 20


def test_no_files_means_no_lines(tmp_path: Path) -> None:
    page = read(tmp_path, days=30)
    assert page == LogPage(lines=(), more=False, cut=False)


@pytest.mark.parametrize("asked", [{"days": 0}, {"days": 31}, {"limit": 0}, {"limit": 501}])
def test_the_query_has_bounds(asked: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="must be"):
        LogQuery(**asked)  # type: ignore[arg-type]


def test_the_folder_reads_from_its_own_today(tmp_path: Path) -> None:
    write(tmp_path, TODAY, [line(1)])
    assert len(LogFolder(tmp_path, today=lambda: TODAY).read(LogQuery()).lines) == 1
    assert LogFolder(tmp_path, today=lambda: TODAY + timedelta(days=1)).read(LogQuery()).lines == ()


# --- GET /api/logs ------------------------------------------------------------------------------


def client(folder: Path | None) -> TestClient:
    reader = LogFolder(folder, today=lambda: TODAY) if folder is not None else None
    return TestClient(create_app(SamplePortfolio(NOW), logs=reader), base_url="http://127.0.0.1")


def test_the_page_says_when_no_logs_are_kept() -> None:
    assert client(None).get("/api/logs").json() == {"kept": False, "lines": [], "more": False, "cut": False}


def test_the_page_gets_each_line_in_parts(tmp_path: Path) -> None:
    write(tmp_path, TODAY, [line(1, level="warning", trace_id=TRACE, span_id="b" * 16, project="zaaheen")])
    body = client(tmp_path).get("/api/logs", params={"level": "warning", "project": "zaaheen"}).json()
    assert body["kept"] is True
    [got] = body["lines"]
    assert got == {
        "timestamp": "2026-10-25T09:01:00Z",
        "level": "warning",
        "event": "push_sent",
        "component": "yaqzan.push",
        "what": "Line 1.",
        "why": "Because.",
        "next": None,
        "trace_id": TRACE,
        "fields": {"bytes": 1, "project": "zaaheen"},
    }


@pytest.mark.parametrize(
    "params",
    [
        {"level": "debug"},
        {"days": "0"},
        {"days": "31"},
        {"limit": "501"},
        {"project": "Zaaheen"},
        {"project": "../etc"},
        {"trace": "xyz"},
        {"event": "push sent"},
        {"agent": "<script>"},
        {"other": "1"},
    ],
)
def test_odd_filters_are_refused(tmp_path: Path, params: dict[str, str]) -> None:
    assert client(tmp_path).get("/api/logs", params=params).status_code == 422


def test_a_folder_that_cannot_be_read_answers_503(tmp_path: Path) -> None:
    class Broken:
        def read(self, _query: LogQuery) -> LogPage:
            raise PermissionError("no")

    app = create_app(SamplePortfolio(NOW), logs=Broken())
    answer = TestClient(app, base_url="http://127.0.0.1").get("/api/logs")
    assert answer.status_code == 503
    assert answer.json() == {"detail": "logs.unreadable"}

"""The local 30-day log file (ADR-034 slice 3): every redacted line, a file a day, never filling the disk."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools import dashboard
from yaqzan.core.sdk import get_logger, install_log_sink, say
from yaqzan.core.telemetry import LogFile
from yaqzan.core.telemetry.logfile import KEEP_DAYS, file_day, file_name

FAKE_TOKEN = "ghp_" + "a" * 36  # built here, never a real token
DAY = datetime(2026, 10, 25, 9, 30, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = DAY) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(autouse=True)
def no_sinks_left() -> Iterator[None]:
    yield
    install_log_sink(None, "file")
    install_log_sink(None, "ship")


def lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def log(name: str = "test") -> object:
    return get_logger(name, sink=io.StringIO())


def test_each_line_is_kept_in_todays_file_after_redaction(tmp_path: Path) -> None:
    kept = LogFile(tmp_path, clock=Clock()).start()
    say(log(), "push_sent", bytes=12, priority="high")
    get_logger("t", sink=io.StringIO()).info("odd", reason=f"bearer {FAKE_TOKEN}")
    kept.stop()
    [first, second] = lines(tmp_path / "yaqzan-2026-10-25.jsonl")
    assert first["event"] == "push_sent"
    assert first["what"] == "A phone alert was sent (12 bytes, priority high)."
    assert first["level"] == "info"
    assert "timestamp" in first
    assert FAKE_TOKEN not in json.dumps(second)


def test_the_file_and_the_log_store_both_get_every_line(tmp_path: Path) -> None:
    shipped: list[dict[str, object]] = []
    install_log_sink(lambda line: shipped.append(dict(line)))
    kept = LogFile(tmp_path, clock=Clock()).start()
    say(log(), "push_sent", bytes=1, priority="low")
    kept.stop()
    assert [s["event"] for s in shipped] == ["push_sent"]
    assert [k["event"] for k in lines(tmp_path / file_name(DAY.date()))] == ["push_sent"]


def test_a_new_day_starts_a_new_file(tmp_path: Path) -> None:
    clock = Clock()
    kept = LogFile(tmp_path, clock=clock).start()
    say(log(), "push_sent", bytes=1, priority="low")
    clock.now = DAY + timedelta(days=1)
    say(log(), "push_sent", bytes=2, priority="low")
    kept.stop()
    assert len(lines(tmp_path / "yaqzan-2026-10-25.jsonl")) == 1
    assert len(lines(tmp_path / "yaqzan-2026-10-26.jsonl")) == 1


def test_the_day_is_the_utc_day_whatever_the_clock_zone(tmp_path: Path) -> None:
    dubai_1am = datetime(2026, 10, 26, 1, 0, tzinfo=timezone(timedelta(hours=4)))  # 25 Oct 21:00 UTC
    kept = LogFile(tmp_path, clock=Clock(dubai_1am)).start()
    say(log(), "push_sent", bytes=1, priority="low")
    kept.stop()
    [path] = tmp_path.iterdir()
    assert path.name == "yaqzan-2026-10-25.jsonl"


def test_files_older_than_30_days_are_deleted_and_nothing_else(tmp_path: Path) -> None:
    today = DAY.date()
    old = tmp_path / file_name(today - timedelta(days=KEEP_DAYS))
    kept_day = tmp_path / file_name(today - timedelta(days=KEEP_DAYS - 1))
    other = tmp_path / "notes-2020-01-01.jsonl"
    for path in (old, kept_day, other):
        path.write_text("{}\n", encoding="utf-8")
    kept = LogFile(tmp_path, clock=Clock()).start()
    say(log(), "push_sent", bytes=1, priority="low")
    kept.stop()
    assert not old.exists()
    assert kept_day.exists()
    assert other.exists()


def test_a_full_day_says_so_once_and_drops_the_rest(tmp_path: Path) -> None:
    shipped: list[dict[str, object]] = []
    install_log_sink(lambda line: shipped.append(dict(line)))
    kept = LogFile(tmp_path, clock=Clock(), max_day_bytes=1_500).start()
    for n in range(20):
        say(log(), "push_sent", bytes=n, priority="low")
    kept.stop()
    written = lines(tmp_path / file_name(DAY.date()))
    assert [w["event"] for w in written].count("log_file_full") == 1
    assert written[-1]["event"] == "log_file_full"
    assert written[-1]["level"] == "warning"
    assert written[-1]["next"]
    assert kept.dropped == 20 - (len(written) - 1)
    assert kept.dropped > 0
    assert "log_file_full" in [s["event"] for s in shipped]  # the log store hears it too
    assert len(shipped) == 21


def test_a_restart_carries_on_in_todays_file(tmp_path: Path) -> None:
    for n in range(2):
        kept = LogFile(tmp_path, clock=Clock()).start()
        say(log(), "push_sent", bytes=n, priority="low")
        kept.stop()
    assert len(lines(tmp_path / file_name(DAY.date()))) == 2


def test_a_restart_on_a_full_day_keeps_nothing_more(tmp_path: Path) -> None:
    (tmp_path / file_name(DAY.date())).write_text("x" * 2_000, encoding="utf-8")
    kept = LogFile(tmp_path, clock=Clock(), max_day_bytes=1_500).start()
    say(log(), "push_sent", bytes=1, priority="low")
    kept.stop()
    assert (tmp_path / file_name(DAY.date())).read_text(encoding="utf-8") == "x" * 2_000
    assert kept.dropped == 1


def test_nothing_is_kept_once_stopped(tmp_path: Path) -> None:
    kept = LogFile(tmp_path, clock=Clock()).start()
    kept.stop()
    say(log(), "push_sent", bytes=1, priority="low")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("name", "day"),
    [
        ("yaqzan-2026-10-25.jsonl", date(2026, 10, 25)),
        ("yaqzan-2026-13-40.jsonl", None),
        ("yaqzan-2026-10-25.jsonl.bak", None),
        ("other-2026-10-25.jsonl", None),
        ("yaqzan-26-10-25.jsonl", None),
    ],
)
def test_only_our_own_day_files_are_ours(name: str, day: date | None) -> None:
    assert file_day(name) == day


# --- the dashboard ------------------------------------------------------------------------------


def test_the_dashboard_keeps_its_lines_while_it_serves_and_stops_after(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ui, logs = tmp_path / "ui", tmp_path / "logs"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html>", encoding="utf-8")

    def serve(app: FastAPI, _host: str, _port: int) -> None:
        TestClient(app, base_url="http://127.0.0.1").get("/api/workspaces")

    assert dashboard.main(["--log-dir", str(logs)], static_dir=ui, serve=serve, environ={}) == 0
    assert f"log lines are kept in {logs}" in capsys.readouterr().out
    [path] = logs.iterdir()
    assert "api_answered" in {line["event"] for line in lines(path)}
    size = path.stat().st_size
    say(log(), "push_sent", bytes=1, priority="low")
    assert path.stat().st_size == size


def test_the_dashboard_will_not_start_when_the_log_folder_cannot_be_made(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html>", encoding="utf-8")
    blocker = tmp_path / "a-file"
    blocker.write_text("", encoding="utf-8")
    served: list[FastAPI] = []
    code = dashboard.main(
        ["--log-dir", str(blocker / "logs")],
        static_dir=ui,
        serve=lambda app, _h, _p: served.append(app),
        environ={},
    )
    assert code == 1
    assert served == []
    assert "cannot keep log files in" in capsys.readouterr().out

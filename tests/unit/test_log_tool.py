"""The chat's ``read_logs`` tool (ADR-034 slice 3): a project's recent warnings and errors, safe fields."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from tools import chat_tools
from tools.log_tool import READ_LOGS_SKILL, log_lines_text, read_logs_tool
from yaqzan.core.llm_gateway import Tool
from yaqzan.core.policy import Standing
from yaqzan.core.registry import Kind, State, load_registry
from yaqzan.core.telemetry import LogFolder, LogPage, LogQuery
from yaqzan.core.telemetry.logfile import file_name

PLUGINS = Path(__file__).resolve().parents[2] / "src" / "yaqzan" / "plugins"
TODAY = date(2026, 10, 25)
NOW = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)
REPOS = {"zaaheen": "acme/zaaheen", "eva": "acme/eva"}


def run(tool: Tool, arg: str) -> str:
    async def go() -> str:
        return await tool.run(arg)

    return asyncio.run(go())


def line(n: int, project: str, level: str = "warning", **more: object) -> dict[str, object]:
    return {
        "timestamp": f"2026-10-25T09:{n:02d}:00Z",
        "level": level,
        "event": "uptime_round_failed",
        "what": f"The check {n} could not run.",
        "why": "The network timed out.",
        "next": "It retries in 5 minutes.",
        "project": project,
        **more,
    }


def folder(tmp_path: Path, by_day: dict[date, list[dict[str, object]]]) -> LogFolder:
    for day, lines in by_day.items():
        (tmp_path / file_name(day)).write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return LogFolder(tmp_path, today=lambda: TODAY)


def test_one_projects_warnings_and_errors_of_today_and_yesterday_newest_first(tmp_path: Path) -> None:
    logs = folder(
        tmp_path,
        {
            TODAY - timedelta(days=2): [line(1, "zaaheen")],
            TODAY - timedelta(days=1): [line(2, "zaaheen", level="error")],
            TODAY: [line(3, "zaaheen", level="info"), line(4, "eva"), line(5, "zaaheen")],
        },
    )
    tool = read_logs_tool(logs, REPOS)
    assert tool is not None
    assert tool.choices == ("eva", "zaaheen")
    assert tool.trusted is False
    assert tool.workspace_of is not None
    assert tool.workspace_of("zaaheen") == "zaaheen"
    text = run(tool, "zaaheen").splitlines()
    assert text[0].startswith("Project: zaaheen. Warnings and errors of today and yesterday")
    assert text[1:] == [
        "2026-10-25T09:05:00Z WARNING uptime_round_failed: The check 5 could not run. "
        "Why: The network timed out. Next: It retries in 5 minutes.",
        "2026-10-25T09:02:00Z ERROR uptime_round_failed: The check 2 could not run. "
        "Why: The network timed out. Next: It retries in 5 minutes.",
    ]


def test_free_text_in_a_line_never_reaches_the_model(tmp_path: Path) -> None:
    logs = folder(tmp_path, {TODAY: [line(1, "zaaheen", exception="Traceback /home/secret", path="/etc/x")]})
    tool = read_logs_tool(logs, REPOS)
    assert tool is not None
    text = run(tool, "zaaheen")
    assert "Traceback" not in text
    assert "/etc/x" not in text


def test_nothing_found_says_so(tmp_path: Path) -> None:
    tool = read_logs_tool(folder(tmp_path, {}), REPOS)
    assert tool is not None
    assert run(tool, "eva").endswith("\nNone found.")


def test_an_unreadable_folder_says_so(tmp_path: Path) -> None:
    class Broken(LogFolder):
        def read(self, query: LogQuery) -> LogPage:
            raise PermissionError("no")

    tool = read_logs_tool(Broken(tmp_path), REPOS)
    assert tool is not None
    assert run(tool, "eva") == "Project: eva. The kept logs could not be read just now."


def test_more_and_cut_are_said() -> None:
    page = LogPage(lines=({"level": "error", "event": "x"},), more=True, cut=True)
    text = log_lines_text("eva", page)
    assert "x: (no sentence)" in text
    assert "Only the newest 1 are shown; there are more." in text
    assert "only its newest part was searched" in text


def test_no_tool_without_kept_logs_or_projects(tmp_path: Path) -> None:
    assert read_logs_tool(None, REPOS) is None
    assert read_logs_tool(LogFolder(tmp_path), {}) is None


def standing(read_logs: State) -> Standing:
    registry = load_registry(PLUGINS)
    states = {(Kind.SKILL, s.id): State.ON for s in registry.skills}
    states[(Kind.SKILL, READ_LOGS_SKILL)] = read_logs
    return Standing(states=states, pending=())


def names(tools: list[Tool]) -> list[str]:
    return [t.name for t in tools]


def test_the_chat_gets_the_tool_only_while_the_skill_is_on_and_logs_are_kept(tmp_path: Path) -> None:
    registry, logs = load_registry(PLUGINS), LogFolder(tmp_path)

    def tools(**more: object) -> list[str]:
        found, _ = chat_tools.tools_for(registry, PLUGINS, None, REPOS, lambda: NOW, **more)  # type: ignore[arg-type]
        return names(found)

    assert "read_logs" in tools(logs=logs)
    assert "read_logs" in tools(logs=logs, standing=standing(State.ON))
    assert "read_logs" not in tools(logs=logs, standing=standing(State.OFF))
    assert "read_logs" not in tools(logs=logs, standing=standing(State.CHANGED))
    assert "read_logs" not in tools()


def test_the_skill_is_in_the_catalogue_and_opens(tmp_path: Path) -> None:
    tool, skills = chat_tools.skill_tool(load_registry(PLUGINS), PLUGINS)
    assert tool is not None
    assert "- read-logs: Read Agent Zaaheen's own log lines" in skills
    assert run(tool, READ_LOGS_SKILL).startswith("# Read Agent Zaaheen's logs")

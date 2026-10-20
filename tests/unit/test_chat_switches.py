"""The chat follows the registry switches (ADR-030): only what is on, asked before every question."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from tests.unit.test_llm_tools import Script
from tools import chat_tools, dashboard
from yaqzan.core.policy import Standing
from yaqzan.core.registry import Kind, State, load_registry
from yaqzan.core.store import StoreError, StoreErrorCode

PLUGINS = Path(__file__).resolve().parents[2] / "src" / "yaqzan" / "plugins"
FAKE_TOKEN = SecretStr("github_pat_" + "x" * 30)  # built here, never a real token
SKILL = "triage-failing-build"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def clock() -> datetime:
    return NOW


def standing(*, skill: State = State.ON, github: State = State.ON) -> Standing:
    return Standing(
        states={
            (Kind.SKILL, SKILL): skill,
            (Kind.CONNECTOR, "github"): github,
            (Kind.CONNECTOR, "paddle"): State.ON,
        },
        pending=(),
    )


def names(found: Standing | None) -> list[str]:
    tools, _ = chat_tools.tools_for(
        load_registry(PLUGINS),
        PLUGINS,
        FAKE_TOKEN,
        {"eva": "o/r"},
        clock,
        standing=found,
    )
    return [t.name for t in tools]


def test_without_switches_everything_found_is_offered() -> None:
    assert names(None) == ["load_skill", "failed_build_log"]


def test_everything_on_is_offered() -> None:
    assert names(standing()) == ["load_skill", "failed_build_log"]


@pytest.mark.parametrize("state", [State.OFF, State.CHANGED])
def test_a_skill_that_is_not_on_leaves_the_tool_and_the_catalogue(state: State) -> None:
    tools, catalogue = chat_tools.tools_for(
        load_registry(PLUGINS),
        PLUGINS,
        None,
        {},
        clock,
        standing=standing(skill=state),
    )
    assert tools == []
    assert catalogue == ""


def test_github_off_takes_the_log_tool_away() -> None:
    assert names(standing(github=State.OFF)) == ["load_skill"]


class Switches:
    def __init__(self, found: Standing | Exception) -> None:
        self.found = found
        self.asked = 0

    async def standing(self) -> Standing:
        self.asked += 1
        if isinstance(self.found, Exception):
            raise self.found
        return self.found


def chat(switches: Switches, model: Script) -> chat_tools.SwitchedChat:
    def build(found: Standing) -> chat_tools.ToolSet:
        return chat_tools.tools_for(
            load_registry(PLUGINS),
            PLUGINS,
            FAKE_TOKEN,
            {"eva": "o/r"},
            clock,
            standing=found,
        )

    return chat_tools.SwitchedChat(model, chat_tools.Switched(build=build, standing=switches.standing))


def system_prompt(model: Script) -> str:
    return model.sent[-1][0]["content"]


def test_the_switches_are_asked_before_every_question() -> None:
    switches = Switches(standing())
    model = Script("first", "second")
    talk = chat(switches, model)
    assert asyncio.run(talk.ask("q", "facts")).answer == "first"
    assert "load_skill" in system_prompt(model)
    switches.found = standing(skill=State.OFF, github=State.OFF)
    assert asyncio.run(talk.ask("q", "facts")).answer == "second"
    assert "load_skill" not in system_prompt(model)
    assert "failed_build_log" not in system_prompt(model)
    assert switches.asked == 2


def test_unreadable_switches_mean_no_tools_at_all() -> None:
    model = Script("answer")
    reply = asyncio.run(
        chat(Switches(StoreError(StoreErrorCode.UNAVAILABLE, "down")), model).ask("q", "facts")
    )
    assert reply.answer == "answer"
    assert "load_skill" not in system_prompt(model)
    assert "failed_build_log" not in system_prompt(model)


def test_model_chat_follows_the_switches_when_given_them() -> None:
    switched = chat_tools.Switched(build=lambda _s: ([], ""), standing=Switches(standing()).standing)
    assert isinstance(
        chat_tools.model_chat("http://127.0.0.1:8766", switched=switched), chat_tools.SwitchedChat
    )


def test_the_dashboard_says_how_to_start_the_laptop_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")

    async def not_running() -> None:
        return None

    code = dashboard.main(["--switches"], static_dir=tmp_path, serve=lambda *_: None, prepare=not_running)
    assert code == 1
    assert "uv run python -m tools.laptop_db up" in capsys.readouterr().out
